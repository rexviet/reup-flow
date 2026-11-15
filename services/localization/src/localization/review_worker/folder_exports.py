"""Save-to-folder requests written by the dashboard (`folder_exports`): copy a finished
video and its SRT from MinIO to `REVIEW_WORKER_EXPORT_ROOT/<target_subdir>/<file_stem>.mp4`
(and `.srt`) on this Mac.

The dashboard's API key ships in the web bundle, so nothing in a row is trusted:

- every directory under the root is opened one level at a time with `O_NOFOLLOW`, so a
  symlink anywhere in `target_subdir` stops the export instead of leading out of the root
  (stronger than checking `realpath` first, which a swap after the check would defeat);
- a file is written to a fresh `.reup-export-<random>.part` opened with
  `O_CREAT | O_EXCL | O_NOFOLLOW` in that directory, then renamed over the final name, so
  a planted symlink is neither followed nor written through;
- a key is downloaded only when it lies where the dashboard itself would serve it from:
  the generation's review folder, or the legacy `output/<aweme_id>.mp4` for the final.

`files` records what was written as paths relative to the export root (`<subdir>/<name>`),
and `error` is a fixed sentence: neither ever names a path on this machine.
"""

import contextlib
import errno
import logging
import os
import re
import threading
import uuid
from pathlib import Path

import psycopg
from botocore.exceptions import ClientError
from psycopg.types.json import Jsonb

from localization.review_worker.final_render import disk_ok
from localization.storage.object_store import ArtifactMissingError

log = logging.getLogger("review_worker.export")

LEASE_MINUTES = 30
PART = re.compile(r"\.reup-export-[0-9a-f]{32}\.part")
STEM = re.compile(r"(E[0-9]{2,}-)?[0-9]+")
# A plain name that does not start with a dot (so never `.`, `..` or a hidden file).
NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SUBDIR_MAX = 512
# POSIX only: Windows has none of these, and the module is imported by every worker,
# including the Windows render PC, which must still start. Exports run only where all three
# exist (the Mac); elsewhere `drain` refuses instead of writing without O_NOFOLLOW.
SUPPORTED = all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"))
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
DIR_FLAGS = os.O_RDONLY | O_DIRECTORY | O_CLOEXEC
PART_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | O_NOFOLLOW | O_CLOEXEC

# SKIP LOCKED: the lock lives only as long as this one autocommit statement, which is all
# it needs; a second claimer passes over the row instead of waiting and then re-checking
# nothing but `id`, which would hand it the same row.
CLAIM_SQL = """
UPDATE folder_exports f
   SET state = 'exporting', claim = gen_random_uuid(), error = NULL,
       lease_expires_at = now() + make_interval(mins => %(lease)s)
 WHERE f.id = (
         SELECT c.id FROM folder_exports c
          WHERE c.state = 'pending' OR (c.state = 'exporting' AND c.lease_expires_at < now())
          ORDER BY c.id
          LIMIT 1
            FOR UPDATE SKIP LOCKED)
RETURNING f.id, f.aweme_id, f.generation, f.target_subdir, f.file_stem, f.final_key,
          f.srt_key, f.claim
"""

RENEW_SQL = """
UPDATE folder_exports SET lease_expires_at = now() + make_interval(mins => %(lease)s)
 WHERE id = %(id)s AND claim = %(claim)s AND state = 'exporting'
"""

SETTLE_SQL = """
UPDATE folder_exports
   SET state = %(state)s, error = %(error)s, files = %(files)s, handled_at = now(),
       claim = NULL, lease_expires_at = NULL
 WHERE id = %(id)s AND claim = %(claim)s AND state = 'exporting'
"""

STALE_SQL = """
SELECT DISTINCT target_subdir FROM folder_exports
 WHERE state = 'exporting' AND lease_expires_at < now()
"""


class ExportRejected(Exception):
    """A reason this request can never be carried out; the message is shown on the dashboard."""


def random_token() -> str:
    return uuid.uuid4().hex


def claim(conn) -> dict | None:
    row = conn.execute(CLAIM_SQL, {"lease": LEASE_MINUTES}).fetchone()
    return dict(row) if row else None


def renew(conn, ids: dict) -> bool:
    return conn.execute(RENEW_SQL, {**ids, "lease": LEASE_MINUTES}).rowcount == 1


# -- what a row may ask for -----------------------------------------------------


def check_subdir(subdir: str) -> list[str]:
    """The segments of a relative directory, by the same rule as the database's CHECK."""
    parts = subdir.split("/")
    if (
        not 1 <= len(subdir) <= SUBDIR_MAX
        or CONTROL.search(subdir)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ExportRejected("Thư mục lưu không hợp lệ.")
    return parts


def check_stem(stem: str, aweme: str) -> None:
    if not STEM.fullmatch(stem) or re.sub(r"^E[0-9]+-", "", stem) != aweme:
        raise ExportRejected("Tên tệp không hợp lệ.")


def check_key(key: str, aweme: str, generation: int, ext: str, *, legacy_ok: bool) -> None:
    """The same rule as the dashboard's `resolveRenderKey`: one plain name directly in
    `output/review/<aweme>/g<generation>/`, or `output/<aweme>.mp4` for a legacy final."""
    if legacy_ok and ext == ".mp4" and key == f"output/{aweme}.mp4":
        return
    folder = f"output/review/{aweme}/g{generation}/"
    name = key[len(folder) :] if key.startswith(folder) else ""
    if not NAME.fullmatch(name) or not name.endswith(ext):
        what = "bản cuối" if ext == ".mp4" else "SRT"
        raise ExportRejected(f"Khóa {what} không hợp lệ, không tải.")


# -- the files ------------------------------------------------------------------


def open_target(root: Path, parts: list[str], *, create: bool) -> int:
    """A descriptor of `root/<parts>`, each level opened without following a symlink.
    Creates missing levels when `create`; never creates the root itself."""
    try:
        fd = os.open(root, DIR_FLAGS)
    except FileNotFoundError:
        raise ExportRejected("Thư mục gốc trên Mac không tồn tại.") from None
    try:
        for part in parts:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=fd)
            try:
                child = os.open(part, DIR_FLAGS | O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ExportRejected(
                        "Thư mục lưu đi qua liên kết hoặc tệp, không ghi."
                    ) from None
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_object(store, key: str, dir_fd: int, name: str, *, require_bytes: bool) -> None:
    """Download `key` to `name` in the directory `dir_fd`, replacing what is there."""
    tmp = f".reup-export-{random_token()}.part"
    fd = os.open(tmp, PART_FLAGS, 0o644, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as out:
            store.download_fileobj(key, out, produced_by="final_render")
            out.flush()
            os.fsync(out.fileno())
            size = os.fstat(out.fileno()).st_size
        if require_bytes and size == 0:
            raise ExportRejected("Bản cuối trên MinIO rỗng, không lưu.")
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)  # the rename itself survives a power cut before the row says done
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def reason(exc: Exception, srt_key: str | None) -> str:
    """A sentence for the dashboard; never the exception text, which may hold a path."""
    if isinstance(exc, ExportRejected):
        return str(exc)
    if isinstance(exc, ArtifactMissingError):
        what = "SRT" if exc.key == srt_key else "bản cuối"
        return f"Không tìm thấy {what} trên MinIO."
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "unknown"))
        if code in ("403", "AccessDenied"):
            return "Worker không có quyền đọc output/ trên MinIO."
        return f"Lỗi MinIO ({code[:40]})."
    if isinstance(exc, OSError):
        code = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
        return f"Không ghi được tệp vào thư mục lưu ({code})."
    return f"Lưu thất bại ({type(exc).__name__})."


def export_claimed(row: dict, *, conn, store, root: Path) -> str:
    """Carry out one claimed row. Returns done, failed, or lost (the claim went elsewhere)."""
    ids = {"id": row["id"], "claim": row["claim"]}
    aweme, gen, stem, subdir = (
        row["aweme_id"], row["generation"], row["file_stem"], row["target_subdir"],
    )  # fmt: skip
    files: list[str] = []
    try:
        check_stem(stem, aweme)
        parts = check_subdir(subdir)
        planned = [(row["final_key"], ".mp4")]
        check_key(row["final_key"], aweme, gen, ".mp4", legacy_ok=True)
        if row["srt_key"] is not None:
            check_key(row["srt_key"], aweme, gen, ".srt", legacy_ok=False)
            planned.append((row["srt_key"], ".srt"))
        dir_fd = open_target(root, parts, create=True)
        try:
            for key, ext in planned:
                if not renew(conn, ids):
                    return "lost"
                write_object(store, key, dir_fd, stem + ext, require_bytes=ext == ".mp4")
                files.append(f"{subdir}/{stem}{ext}")
        finally:
            os.close(dir_fd)
        state, error = "done", None
    except psycopg.Error:
        raise  # the lease runs out and the row is claimed again
    except Exception as exc:  # noqa: BLE001
        state, error = "failed", reason(exc, row["srt_key"])
        log.warning("export %s/g%s failed: %s (%s)", aweme, gen, type(exc).__name__, error)
    settled = conn.execute(
        SETTLE_SQL,
        {**ids, "state": state, "error": error, "files": Jsonb(files) if files else None},
    ).rowcount
    return state if settled == 1 else "lost"


def drain(conn, store, root: Path, *, min_free_gb: float, stopping: threading.Event) -> int:
    """Export open rows one after another until none is left, the disk under the root is
    low, or the worker stops. Returns how many rows it settled or lost.

    First removes the temp files of exports whose lease ran out, which are the rows about
    to be claimed again: their worker was stopped or killed mid-download."""
    if not SUPPORTED:
        log.warning("folder exports need POSIX O_NOFOLLOW; not exporting on this platform")
        return 0
    removed = clean_stale(conn, root)
    if removed:
        log.info("removed %s stale export temp file(s)", removed)
    handled = 0
    while not stopping.is_set():
        try:
            if not disk_ok(root, min_free_gb)[0]:
                break
        except OSError:
            pass  # a missing root fails each row with a sentence the dashboard can show
        row = claim(conn)
        if row is None:
            break
        outcome = export_claimed(row, conn=conn, store=store, root=root)
        log.info("export %s/g%s: %s", row["aweme_id"], row["generation"], outcome)
        handled += 1
    return handled


def clean_stale(conn, root: Path) -> int:
    """Remove the temporary files of exports whose worker died (their lease ran out) from
    those exports' folders. Returns how many it removed."""
    removed = 0
    for row in conn.execute(STALE_SQL).fetchall():
        try:
            fd = open_target(root, check_subdir(row["target_subdir"]), create=False)
        except (ExportRejected, OSError):
            continue
        try:
            for name in os.listdir(fd):
                if not PART.fullmatch(name):
                    continue
                # A directory of that name (possible through `save_subdir`) cannot be
                # unlinked; it must not stop every later export either.
                try:
                    os.unlink(name, dir_fd=fd)
                    removed += 1
                except OSError:
                    continue
        finally:
            os.close(fd)
    return removed
