"""python -m localization.review_worker.main

One loop every 5 seconds: heartbeat, Studio open requests, a scan every 60 seconds and,
when the disk allows, one final render at a time on its own thread.

`REVIEW_WORKER_ROLES` splits this between machines: a Windows PC that only renders runs
`render` (no Studio, no scan of its empty `data/review`); the Mac then runs `studio`.
"""

import logging
import os
import signal
import threading
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from localization import platform
from localization.config import load_settings
from localization.review_bridge.repository import Repository
from localization.review_worker import (
    final_render,
    heartbeat,
    posters,
    review_scan,
    studio_requests,
)
from localization.review_worker.config import WorkerConfig
from localization.storage.object_store import ObjectStore

log = logging.getLogger("review_worker")

TICK_S = 5
SCAN_EVERY_S = 60


def connect(cfg: WorkerConfig) -> psycopg.Connection:
    return psycopg.connect(
        cfg.database_url, autocommit=True, row_factory=dict_row, connect_timeout=5
    )


class Worker:
    def __init__(self, cfg: WorkerConfig, store, *, connector=connect, render=None, probe=None):
        self.cfg = cfg
        self.store = store
        self.repo = Repository(store, cfg.data_root)
        self.connector = connector
        self.render = render or final_render.render_claimed
        self.probe = probe or (lambda: heartbeat.probe_voicestudio(cfg.voicestudio_url))
        self.stopping = threading.Event()
        self.cancelled = threading.Event()
        self.thread: threading.Thread | None = None
        self.studio_thread: threading.Thread | None = None
        self.current: dict | None = None
        self.last_scan = float("-inf")
        self.conn: psycopg.Connection | None = None
        self.studio_env = {
            key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ
        }

    # -- helpers --------------------------------------------------------------

    def db(self) -> psycopg.Connection:
        if self.conn is None or self.conn.closed:
            self.conn = self.connector(self.cfg)
        return self.conn

    def rendering(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _studio_thread(self) -> None:
        conn = None
        try:
            conn = self.connector(self.cfg)
            studio_requests.handle_requests(conn, self.cfg, self.studio_env)
        except Exception as exc:  # noqa: BLE001
            log.error("studio requests failed: %s", type(exc).__name__)
        finally:
            if conn is not None:
                conn.close()

    def _render_thread(self, row: dict) -> None:
        conn = None
        try:
            conn = final_render.Session(lambda: self.connector(self.cfg))
            outcome = self.render(
                row, conn=conn, store=self.store, repo=self.repo, cfg=self.cfg,
                cancelled=self.cancelled,
            )  # fmt: skip
            log.info("render %s/g%s: %s", row["aweme_id"], row["generation"], outcome)
        except Exception as exc:  # noqa: BLE001
            # The lease expires and the row is claimed again, up to the attempt limit.
            log.error("render %s/g%s crashed: %s", row["aweme_id"], row["generation"],
                      type(exc).__name__)  # fmt: skip
        finally:
            self.current = None
            if conn is not None:
                conn.close()

    # -- one loop -------------------------------------------------------------

    def tick(self) -> None:
        conn = self.db()
        disk_path = heartbeat.data_disk(self.cfg.data_root)
        enough, free, total = final_render.disk_ok(disk_path, self.cfg.min_free_gb)
        paused = None
        if not enough:
            paused = f"Đĩa còn dưới {self.cfg.min_free_gb:g} GB, chưa nhận việc render."
        elif self.stopping.is_set():
            paused = "Worker đang dừng."
        heartbeat.beat(
            conn,
            heartbeat.build_info(
                disk_free=free, disk_total=total, voicestudio=self.probe(),
                current=self.current, paused_reason=paused,
            ),
            self.cfg.name,
        )  # fmt: skip
        if self.stopping.is_set():
            return
        if "studio" in self.cfg.roles:
            self._studio_work(conn)
        if "render" in self.cfg.roles and enough and not self.rendering():
            self._claim(conn)

    def _studio_work(self, conn) -> None:
        # Opening Studio can take over a minute; it must not delay the heartbeat.
        if self.studio_thread is None or not self.studio_thread.is_alive():
            self.studio_thread = threading.Thread(
                target=self._studio_thread, name="studio-requests", daemon=True
            )
            self.studio_thread.start()
        if time.monotonic() - self.last_scan >= SCAN_EVERY_S:
            self.last_scan = time.monotonic()
            review_scan.scan(conn, self.repo)

    def _claim(self, conn) -> None:
        row = final_render.claim(conn, self.cfg)
        if not row:
            return
        self.current = {
            "aweme_id": row["aweme_id"],
            "generation": row["generation"],
            "state": "rendering",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self.thread = threading.Thread(
            target=self._render_thread, args=(row,), name="final-render", daemon=True
        )
        self.thread.start()

    def shutdown(self) -> None:
        """Wait for the running render, then give its row back and kill the CLI."""
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(self.cfg.shutdown_grace_s)
            if self.thread.is_alive():
                self.cancelled.set()
                self.thread.join(30)
        if self.studio_thread is not None:
            self.studio_thread.join(5)
        if self.conn is not None and not self.conn.closed:
            self.conn.close()

    def _backfill_posters(self) -> None:
        """Pictures for the videos rendered before posters existed; once per start, apart from
        the loop so that the heartbeat is never held up by a download."""
        conn = None
        try:
            conn = self.connector(self.cfg)
            made = posters.backfill(conn, self.store, self.stopping)
            log.info("poster backfill made %s poster(s)", made)
        except Exception as exc:  # noqa: BLE001
            log.warning("poster backfill failed: %s", type(exc).__name__)
        finally:
            if conn is not None:
                conn.close()

    def run(self) -> None:
        if "render" in self.cfg.roles:
            final_render.clean_stale(self.cfg)
        if "studio" in self.cfg.roles:
            threading.Thread(
                target=self._backfill_posters, name="poster-backfill", daemon=True
            ).start()
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                log.error("loop failed: %s", type(exc).__name__)
                if self.conn is not None:
                    self.conn.close()
                    self.conn = None
            self.stopping.wait(TICK_S)
        self.shutdown()


def stop_signals() -> tuple[int, ...]:
    """SIGTERM (pm2) and SIGINT (Ctrl+C) everywhere; on Windows also SIGBREAK
    (Ctrl+Break, and what a service wrapper sends to a console program)."""
    extra = getattr(signal, "SIGBREAK", None)
    return (signal.SIGTERM, signal.SIGINT) + ((extra,) if extra is not None else ())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per heartbeat otherwise
    host = platform.current()  # an unknown REVIEW_PLATFORM stops here, before any work
    cfg = WorkerConfig.from_environ()
    store = ObjectStore.from_settings(load_settings())
    cfg.data_root.mkdir(parents=True, exist_ok=True)
    worker = Worker(cfg, store)
    for sig in stop_signals():
        signal.signal(sig, lambda *_: worker.stopping.set())
    log.info(
        "review worker started; data root %s; roles %s; name %s; platform %s",
        Path(cfg.data_root), ",".join(sorted(cfg.roles)), cfg.name, host.name,
    )  # fmt: skip
    worker.run()


if __name__ == "__main__":
    main()
