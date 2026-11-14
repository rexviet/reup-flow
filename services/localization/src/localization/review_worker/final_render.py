"""Claim an approved revision, render exactly that revision outside the Studio
projection, upload the MP4 and SRT, and record the result under the claim token."""

import contextlib
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import psycopg

from localization.platform import ProcessControl
from localization.platform import current as current_platform
from localization.platform.posix import group_alive  # noqa: F401  (kept importable here)
from localization.review_worker import posters
from localization.review_worker.config import WorkerConfig

log = logging.getLogger("review_worker.render")

# A row is claimable only while an `approved` decision for the same revision exists.
CLAIM_SQL = """
UPDATE localization.final_renders f
   SET state = 'rendering', attempts = f.attempts + 1, claim = gen_random_uuid(),
       lease_expires_at = now() + make_interval(mins => %(lease)s), error = NULL
 WHERE (f.aweme_id, f.generation) = (
         SELECT c.aweme_id, c.generation
           FROM localization.final_renders c
           JOIN review_decisions d
             ON d.aweme_id = c.aweme_id AND d.generation = c.generation
            AND d.decision = 'approved' AND d.approved_revision = c.approved_revision
          WHERE (c.state = 'queued'
                 OR (c.state = 'rendering' AND c.lease_expires_at < now()))
            AND c.attempts < %(max_attempts)s
          ORDER BY c.lease_expires_at NULLS FIRST, c.aweme_id, c.generation
          LIMIT 1
            FOR UPDATE OF c SKIP LOCKED)
RETURNING f.aweme_id, f.generation, f.approved_revision, f.claim, f.attempts
"""

# A render whose lease lapsed after its last attempt would otherwise stay `rendering`.
REAP_SQL = """
UPDATE localization.final_renders
   SET state = 'failed', claim = NULL, lease_expires_at = NULL,
       error = %(message)s
 WHERE state = 'rendering' AND lease_expires_at < now() AND attempts >= %(max_attempts)s
"""

# Also the cheap "is this still wanted" check: false once the approval is gone.
RENEW_SQL = """
UPDATE localization.final_renders f
   SET lease_expires_at = now() + make_interval(mins => %(lease)s)
 WHERE f.aweme_id = %(aweme)s AND f.generation = %(gen)s AND f.claim = %(claim)s
   AND f.state = 'rendering'
   AND EXISTS (SELECT 1 FROM review_decisions d
                WHERE d.aweme_id = f.aweme_id AND d.generation = f.generation
                  AND d.decision = 'approved' AND d.approved_revision = f.approved_revision)
"""

DONE_SQL = """
UPDATE localization.final_renders f
   SET state = 'rendered', final_key = %(final)s, srt_key = %(srt)s, rendered_at = now(),
       error = NULL, claim = NULL, lease_expires_at = NULL
 WHERE f.aweme_id = %(aweme)s AND f.generation = %(gen)s AND f.claim = %(claim)s
   AND f.state = 'rendering'
   AND EXISTS (SELECT 1 FROM review_decisions d
                WHERE d.aweme_id = f.aweme_id AND d.generation = f.generation
                  AND d.decision = 'approved' AND d.approved_revision = f.approved_revision)
"""

FAIL_SQL = """
UPDATE localization.final_renders
   SET state = 'failed', error = %(error)s, claim = NULL, lease_expires_at = NULL
 WHERE aweme_id = %(aweme)s AND generation = %(gen)s AND claim = %(claim)s
   AND state = 'rendering'
"""

RETRY_SQL = """
UPDATE localization.final_renders
   SET state = 'queued', error = %(error)s, claim = NULL, lease_expires_at = NULL
 WHERE aweme_id = %(aweme)s AND generation = %(gen)s AND claim = %(claim)s
   AND state = 'rendering'
"""

# Shutdown: the interrupted attempt is not the video's fault, so it is given back.
REQUEUE_SQL = """
UPDATE localization.final_renders
   SET state = 'queued', attempts = GREATEST(attempts - 1, 0), claim = NULL,
       lease_expires_at = NULL
 WHERE aweme_id = %(aweme)s AND generation = %(gen)s AND claim = %(claim)s
   AND state = 'rendering'
"""


class Session:
    """A connection that reopens itself once when the database drops it, so a blip during
    a long render neither loses the lease renewal nor the final write."""

    def __init__(self, connect):
        self._connect = connect
        self.conn = connect()

    def execute(self, sql, params=None):
        for attempt in (0, 1):
            try:
                if self.conn.closed or self.conn.broken:
                    self.conn = self._connect()
                return self.conn.execute(sql, params)
            except psycopg.OperationalError:
                if attempt:
                    raise
                self.close()
                self.conn = self._connect()

    def close(self):
        with contextlib.suppress(psycopg.Error):
            self.conn.close()


class RenderRejected(Exception):
    """A reason the approved revision can never be rendered; retrying will not help."""


class ClaimLost(Exception):
    """The row was returned, re-approved or re-claimed while rendering."""


class Cancelled(Exception):
    """The worker is shutting down."""


def disk_ok(path: Path, min_free_gb: float) -> tuple[bool, int, int]:
    usage = shutil.disk_usage(path)
    return usage.free >= min_free_gb * 1024**3, usage.free, usage.total


def claim(conn, cfg: WorkerConfig) -> dict | None:
    conn.execute(
        REAP_SQL,
        {
            "max_attempts": cfg.max_attempts,
            "message": f"Render quá thời gian chờ sau {cfg.max_attempts} lần thử.",
        },
    )
    row = conn.execute(
        CLAIM_SQL, {"lease": cfg.lease_minutes, "max_attempts": cfg.max_attempts}
    ).fetchone()
    return dict(row) if row else None


def render_command(cfg: WorkerConfig, folder: Path, output: Path) -> list[str]:
    if cfg.hyperframes_bin is None:
        raise RuntimeError("HYPERFRAMES_BIN is not set")
    command = [
        cfg.node, cfg.hyperframes_bin, "render", str(folder),
        "-o", str(output), "-q", "delivery", "--workers", str(cfg.render_workers),
    ]  # fmt: skip
    if cfg.render_gpu:
        command.append("--gpu")
    return command


def render_env(tmp: Path) -> dict[str, str]:
    """Nothing but what the CLI and Chrome need: no database or storage credentials."""
    return current_platform().process.child_env(tmp)


def kill_group(proc: subprocess.Popen, process: ProcessControl | None = None) -> None:
    """Stop the renderer and everything it started (Chrome, ffmpeg), also when the leader
    already exited and left children behind."""
    (process or current_platform().process).terminate_tree(proc)


def run_cli(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    timeout_s: float,
    renew_every_s: float,
    renew: Callable[[], bool],
    cancelled: threading.Event,
    process: ProcessControl | None = None,
) -> None:
    """Run the renderer in its own process group; kill the group on timeout, lost
    claim or cancellation."""
    process = process or current_platform().process
    with log_path.open("wb") as out:
        proc = process.spawn(command, cwd=cwd, env=env, stdout=out)
    started = last_renew = time.monotonic()
    try:
        while True:
            try:
                code = proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if cancelled.is_set():
                raise Cancelled()
            if now - started > timeout_s:
                raise TimeoutError(f"Render quá {int(timeout_s // 60)} phút, đã dừng.")
            if now - last_renew >= renew_every_s:
                last_renew = now
                if not renew():
                    raise ClaimLost()
    finally:
        kill_group(proc, process)
    if code != 0:
        raise RuntimeError(f"hyperframes render thoát với mã {code}: {tail(log_path)}")


def tail(path: Path, size: int = 600) -> str:
    try:
        data = path.read_bytes()[-size:]
    except OSError:
        return ""
    return " ".join(data.decode("utf-8", "replace").split())[-size:]


def short(message: str, limit: int = 500) -> str:
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[: limit - 1] + "…"


def output_keys(aweme: str, generation: int, revision: str) -> tuple[str, str]:
    base = f"output/review/{aweme}/g{generation}/{revision}"
    return base + ".mp4", base + ".srt"


def _upload_poster(store, video: Path, final_key: str, work: Path) -> None:
    """The video's still, beside it. Best effort: a render does not fail for its picture."""
    key = posters.poster_key(final_key)
    if key is None:
        return
    try:
        out = work / "poster.jpg"
        if posters.make_poster(video, out):
            store.put_file(key, out, content_type="image/jpeg")
    except Exception as exc:  # noqa: BLE001
        log.warning("poster of a render was not stored: %s", type(exc).__name__)


def render_claimed(
    row: dict,
    *,
    conn,
    store,
    repo,
    cfg: WorkerConfig,
    cancelled: threading.Event,
    runner: Callable[..., None] = run_cli,
) -> str:
    """Returns the final state of the claim: rendered, failed, queued or dropped."""
    aweme, gen, revision, token = (
        row["aweme_id"], row["generation"], row["approved_revision"], row["claim"],
    )  # fmt: skip
    ids = {"aweme": aweme, "gen": gen, "claim": token}
    cfg.work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="final-render-", dir=cfg.work_root))
    try:
        _, project, _ = repo.current(aweme, gen)
        if project.revision_id != revision:
            raise RenderRejected("Bản đã đổi sau khi duyệt, duyệt lại bản mới.")
        folder = work / "project"
        repo.export_revision(aweme, gen, revision, folder)
        if cancelled.is_set():
            raise Cancelled()
        tmp = work / "tmp"
        tmp.mkdir()
        output = work / "final.mp4"
        runner(
            render_command(cfg, folder, output),
            cwd=folder,
            env=render_env(tmp),
            log_path=work / "render.log",
            timeout_s=cfg.render_timeout_s,
            renew_every_s=cfg.renew_every_s,
            renew=lambda: (
                conn.execute(RENEW_SQL, {**ids, "lease": cfg.lease_minutes}).rowcount == 1
            ),
            cancelled=cancelled,
        )
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("Render không tạo ra tệp MP4.")
        # Still ours and still approved? Then upload; otherwise nobody wants the result.
        if cancelled.is_set():
            raise Cancelled()
        if conn.execute(RENEW_SQL, {**ids, "lease": cfg.lease_minutes}).rowcount != 1:
            raise ClaimLost()
        final_key, srt_key = output_keys(aweme, gen, revision)
        store.put_file(final_key, output, content_type="video/mp4")
        store.put_bytes(
            srt_key, (folder / "captions.srt").read_bytes(), content_type="application/x-subrip"
        )
        _upload_poster(store, output, final_key, work)
        done = conn.execute(DONE_SQL, {**ids, "final": final_key, "srt": srt_key}).rowcount
        if done == 1:
            return "rendered"
        # Still our claim but the approval no longer matches: nobody wants this result.
        conn.execute(
            FAIL_SQL, {**ids, "error": "Quyết định duyệt đã đổi trong lúc render, kết quả bị bỏ."}
        )
        return "dropped"
    except Cancelled:
        conn.execute(REQUEUE_SQL, ids)
        return "queued"
    except ClaimLost:
        # Our token still matching means the approval went away; a lost token matches nothing.
        conn.execute(
            FAIL_SQL, {**ids, "error": "Quyết định duyệt đã đổi trong lúc render, kết quả bị bỏ."}
        )
        return "dropped"
    except RenderRejected as exc:
        conn.execute(FAIL_SQL, {**ids, "error": short(exc)})
        return "failed"
    except Exception as exc:  # noqa: BLE001
        log.warning("render of %s/g%s failed: %s", aweme, gen, type(exc).__name__)
        sql = RETRY_SQL if row["attempts"] < cfg.max_attempts else FAIL_SQL
        conn.execute(sql, {**ids, "error": short(f"{type(exc).__name__}: {exc}")})
        return "queued" if sql is RETRY_SQL else "failed"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def clean_stale(cfg: WorkerConfig) -> None:
    """Directories left by a worker that was killed outright; a younger one may belong
    to a render that is still running."""
    if not cfg.work_root.is_dir():
        return
    cutoff = time.time() - cfg.lease_minutes * 60
    for child in cfg.work_root.iterdir():
        if child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
