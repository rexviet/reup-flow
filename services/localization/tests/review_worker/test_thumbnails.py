"""The thumbnail of every downloaded video: one per pass, newest first, cut safely from a private
copy of the source, and never in the way of the heartbeat."""

import dataclasses
import shutil
import stat
import threading
import time
from pathlib import Path

import pytest

from localization.config import ConfigError
from localization.review_worker import review_scan, thumbnails
from localization.review_worker.config import WorkerConfig
from localization.review_worker.main import Worker
from localization.review_worker.thumbnails import Thumbnailer, source_key, thumb_key
from tests.review_worker.test_posters import is_jpeg, real_mp4

GRANTS = (
    Path(__file__).resolve().parents[2] / "migrations" / "261026-outbox-starter-grants.sql"
).read_text()


class Rows:
    """A connection whose only query is the candidate list."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    def execute(self, sql):
        assert sql == thumbnails.CANDIDATES_SQL
        self.queries += 1
        return self

    def fetchall(self):
        return self.rows


def row(aweme, bucket="review-test", key=None):
    return {"aweme_id": aweme, "storage_path": f"s3://{bucket}/{key or f'source/ch/{aweme}.mp4'}"}


@pytest.fixture
def source(review_store, tmp_path):
    """Stores a real MP4 as the source of each given id."""

    def put(*awemes, data: bytes | None = None):
        video = real_mp4(tmp_path / "src.mp4")
        for aweme in awemes:
            key = f"source/ch/{aweme}.mp4"
            if data is None:
                review_store.put_file(key, video)
            else:
                review_store.put_bytes(key, data)

    return put


def thumbnailer(store, tmp_path, **kwargs):
    return Thumbnailer(store, tmp_path / "work" / "thumbs", min_free_gb=0, **kwargs)


def leftovers(t: Thumbnailer) -> list[Path]:
    return list(t.work_dir.glob(f"{thumbnails.TEMP_PREFIX}*"))


def test_keys_are_built_from_checked_values_only():
    assert thumb_key("7001") == "output/thumbs/7001.jpg"
    with pytest.raises(ValueError):
        thumb_key("../7001")
    assert source_key("s3://b/source/ch/1.mp4", "b") == "source/ch/1.mp4"
    for bad in ("s3://other/source/ch/1.mp4", "s3://b/work/1.mp4", "s3://b/source/../x", "/x"):
        with pytest.raises(ValueError):
            source_key(bad, "b")


def test_one_video_per_pass_newest_first_as_a_small_jpeg(review_store, source, tmp_path):
    source("7001", "7003")
    t = thumbnailer(review_store, tmp_path)
    conn = Rows([row("7003"), row("7001")])
    assert t.run_pass(conn, threading.Event()) == "7003"
    assert not review_store.exists(thumb_key("7001"))
    data = review_store.get_bytes(thumb_key("7003"), produced_by="t")
    assert is_jpeg(data) and len(data) < 100_000
    assert t.run_pass(conn, threading.Event()) == "7001"
    # Nothing left: the pass says so and waits before the next one.
    assert t.run_pass(conn, threading.Event()) is None
    assert not t.due()
    assert leftovers(t) == []


def test_an_existing_thumbnail_is_not_made_again(review_store, source, tmp_path):
    source("7001")
    review_store.put_bytes(thumb_key("7001"), b"\xff\xd8\xffold")
    cuts = []
    t = thumbnailer(review_store, tmp_path, cut=lambda *a, **k: cuts.append(1) or False)
    assert t.run_pass(Rows([row("7001")]), threading.Event()) is None
    assert cuts == [] and "7001" in t.done
    assert review_store.get_bytes(thumb_key("7001"), produced_by="t") == b"\xff\xd8\xffold"


def test_a_source_that_is_not_an_mp4_never_reaches_ffmpeg(review_store, source, tmp_path):
    source("7002", data=b"<html>not a video</html>")
    source("7001")
    cuts = []

    def cut(video, out, **kwargs):
        cuts.append(video)
        return thumbnails.cut_frame(video, out, **kwargs)

    t = thumbnailer(review_store, tmp_path, cut=cut)
    conn = Rows([row("7002"), row("7001")])
    assert t.run_pass(conn, threading.Event()) is None
    assert cuts == [] and "7002" in t.failed and leftovers(t) == []
    # Remembered: the next pass moves on to the next video instead of trying it again.
    assert t.run_pass(conn, threading.Event()) == "7001"


def test_temp_files_are_private_and_removed_even_when_ffmpeg_fails(review_store, source, tmp_path):
    source("7001", "7002")
    seen = []

    def failing(video, out, **kwargs):
        seen.append((stat.S_IMODE(video.stat().st_mode), stat.S_IMODE(out.stat().st_mode)))
        return False

    def crashing(video, out, **kwargs):
        raise RuntimeError("ffmpeg went away")

    t = thumbnailer(review_store, tmp_path, cut=failing)
    assert t.run_pass(Rows([row("7001")]), threading.Event()) is None
    assert seen == [(0o600, 0o600)] and leftovers(t) == []
    assert stat.S_IMODE(t.work_dir.stat().st_mode) == 0o700
    t.cut = crashing
    assert t.run_pass(Rows([row("7002")]), threading.Event()) is None
    assert t.failed == {"7001", "7002"} and leftovers(t) == []
    assert not review_store.exists(thumb_key("7001"))


def test_a_source_of_another_bucket_or_prefix_is_skipped(review_store, tmp_path):
    t = thumbnailer(review_store, tmp_path)
    conn = Rows([row("7002", bucket="elsewhere"), row("7001", key="work/7001.mp4")])
    assert t.run_pass(conn, threading.Event()) is None
    assert t.failed == {"7001", "7002"}


def test_a_missing_source_fails_that_video_only(review_store, source, tmp_path):
    source("7001")
    t = thumbnailer(review_store, tmp_path)
    conn = Rows([row("7009"), row("7001")])
    assert t.run_pass(conn, threading.Event()) is None
    assert t.failed == {"7009"} and leftovers(t) == []
    assert t.run_pass(conn, threading.Event()) == "7001"


def test_no_ffmpeg_makes_nothing_and_says_so_once(
    review_store, source, tmp_path, monkeypatch, caplog
):
    source("7001")
    monkeypatch.setattr(thumbnails, "find_ffmpeg", lambda configured: None)
    t = thumbnailer(review_store, tmp_path)
    conn = Rows([row("7001")])
    assert t.run_pass(conn, threading.Event()) is None
    t.idle_until = float("-inf")
    assert t.run_pass(conn, threading.Event()) is None
    assert conn.queries == 0 and not t.failed
    assert [r.getMessage() for r in caplog.records if "no ffmpeg" in r.getMessage()] == [
        "no ffmpeg found (set FFMPEG_BIN): no video thumbnails are made"
    ]


def test_a_stop_request_ends_the_pass_before_the_next_video(review_store, source, tmp_path):
    source("7001")
    stop = threading.Event()
    stop.set()
    t = thumbnailer(review_store, tmp_path)
    assert t.run_pass(Rows([row("7001")]), stop) is None
    assert not review_store.exists(thumb_key("7001"))


def test_files_left_by_a_killed_worker_are_removed_on_the_first_pass(review_store, tmp_path):
    t = thumbnailer(review_store, tmp_path)
    t.work_dir.mkdir(parents=True)
    stale = t.work_dir / f"{thumbnails.TEMP_PREFIX}abc.mp4"
    stale.write_bytes(b"x")
    t.run_pass(Rows([]), threading.Event())
    assert not stale.exists()


# -- against the real schema and role ----------------------------------------------------------


def add_video(db, aweme, storage_path):
    db.admin.execute(
        "INSERT INTO videos (aweme_id, sec_user_id, create_time, status, storage_path) "
        "VALUES (%s, 'ch', now(), 'done', %s)",
        (aweme, storage_path),
    )


def test_the_worker_role_reads_the_candidates_newest_first(db):
    db.admin.execute(GRANTS)
    add_video(db, "999", "s3://b/source/ch/999.mp4")
    add_video(db, "7001", "s3://b/source/ch/7001.mp4")
    add_video(db, "7002", None)
    rows = db.worker().execute(thumbnails.CANDIDATES_SQL).fetchall()
    assert [r["aweme_id"] for r in rows] == ["7001", "999"]


def make_worker(db, cfg, store, **kwargs):
    kwargs.setdefault("probe", lambda: {"ok": True, "latency_ms": 3, "checked_at": "now"})
    return Worker(cfg, store, connector=lambda _cfg: db.worker(), **kwargs)


def test_a_slow_thumbnail_never_holds_up_the_heartbeat(db, cfg, review_store, source, monkeypatch):
    db.admin.execute(GRANTS)
    add_video(db, "7001", "s3://review-test/source/ch/7001.mp4")
    source("7001")
    monkeypatch.setattr(review_scan, "scan", lambda *a: None)
    release = threading.Event()

    def slow_cut(video, out, **kwargs):
        release.wait(10)
        return thumbnails.cut_frame(video, out, **kwargs)

    worker = make_worker(db, dataclasses.replace(cfg, roles=frozenset({"studio"})), review_store)
    worker.thumbs.cut = slow_cut
    worker.thumbs.min_free_gb = 0
    started = time.monotonic()
    worker.tick()
    worker.tick()
    assert time.monotonic() - started < 3
    assert worker.thumb_thread.is_alive()
    beats = db.admin.execute("SELECT count(*) AS n FROM localization.host_workers").fetchone()
    assert beats["n"] == 1
    release.set()
    worker.thumb_thread.join(10)
    assert is_jpeg(review_store.get_bytes(thumb_key("7001"), produced_by="t"))
    worker.shutdown()


def test_only_the_studio_role_makes_thumbnails(db, cfg, review_store):
    worker = make_worker(db, dataclasses.replace(cfg, roles=frozenset({"render"})), review_store)
    worker.tick()
    assert worker.thumb_thread is None
    if worker.thread is not None:
        worker.thread.join(5)


def test_ffmpeg_bin_must_be_absolute_and_an_unusable_one_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOC_DATABASE_URL", "postgresql://localization@db/reup")
    env = {"REVIEW_WORKER_DATA_ROOT": str(tmp_path / "data" / "review")}
    assert WorkerConfig.from_environ(env).ffmpeg_bin is None
    cfg = WorkerConfig.from_environ({**env, "FFMPEG_BIN": "/usr/local/bin/ffmpeg"})
    assert cfg.ffmpeg_bin == "/usr/local/bin/ffmpeg"
    with pytest.raises(ConfigError, match="FFMPEG_BIN"):
        WorkerConfig.from_environ({**env, "FFMPEG_BIN": "ffmpeg"})
    assert thumbnails.find_ffmpeg(str(tmp_path / "missing")) == shutil.which("ffmpeg")
