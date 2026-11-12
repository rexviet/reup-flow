"""Studio open requests written by the dashboard. The Node helper replaces the running
Studio (after backing it up) and opens the project."""

import logging
import re
import subprocess

from localization.review_worker.config import WorkerConfig

log = logging.getLogger("review_worker.studio")

PENDING_SQL = """
SELECT id, aweme_id, generation FROM studio_open_requests
 WHERE state = 'pending' ORDER BY id LIMIT 5
"""

SETTLE_SQL = """
UPDATE studio_open_requests SET state = %(state)s, error = %(error)s, handled_at = now()
 WHERE id = %(id)s AND state = 'pending'
"""

IDENTITY = re.compile(r"^[0-9]{1,32}$")
HELPER_TIMEOUT_S = 150


def run_helper(cfg: WorkerConfig, aweme: str, generation: int, env: dict[str, str]) -> None:
    if cfg.studio_script is None:
        raise RuntimeError("REVIEW_WORKER_STUDIO_SCRIPT is not set")
    if not IDENTITY.fullmatch(aweme) or generation < 0:
        raise ValueError("invalid project identity")
    done = subprocess.run(
        [cfg.node, str(cfg.studio_script), aweme, str(generation)],
        env=env, capture_output=True, text=True, timeout=HELPER_TIMEOUT_S,
        stdin=subprocess.DEVNULL, check=False,
    )  # fmt: skip
    if done.returncode != 0:
        detail = " ".join((done.stderr or done.stdout).split())[-300:]
        raise RuntimeError(detail or f"helper exited with {done.returncode}")


def handle_requests(conn, cfg: WorkerConfig, env: dict[str, str], runner=run_helper) -> int:
    handled = 0
    for row in conn.execute(PENDING_SQL).fetchall():
        state, error = "done", None
        try:
            runner(cfg, row["aweme_id"], row["generation"], env)
        except subprocess.TimeoutExpired:
            state, error = "failed", "Mở Studio quá thời gian chờ."
        except Exception as exc:  # noqa: BLE001
            state, error = "failed", " ".join(str(exc).split())[:500] or type(exc).__name__
            log.warning("studio request %s failed", row["id"])
        conn.execute(SETTLE_SQL, {"state": state, "error": error, "id": row["id"]})
        handled += 1
    return handled
