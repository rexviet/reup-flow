"""The still of a rendered video: where it is stored, that a render never fails for it, and
that videos rendered before posters existed get one."""

import logging
import subprocess
import threading
from pathlib import Path

from botocore.exceptions import ClientError

from localization.review_worker import posters
from localization.review_worker.final_render import claim, render_claimed
from localization.review_worker.posters import backfill, make_poster, poster_key


def real_mp4(path: Path, seconds: float = 2.0) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=640x360:rate=10",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def mp4_runner(command, *, cwd, env, log_path, **_):
    real_mp4(Path(command[command.index("-o") + 1]))


def is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def test_the_poster_sits_beside_its_video_and_only_an_mp4_has_one():
    assert poster_key("output/review/7001/g1/abc.mp4") == "output/review/7001/g1/abc.jpg"
    assert poster_key("output/review/7001/g1/abc.srt") is None
    assert poster_key("output/7001.mp4") == "output/7001.jpg"


def test_a_poster_is_a_jpeg_at_the_set_width(tmp_path):
    video = real_mp4(tmp_path / "v.mp4")
    out = tmp_path / "p.jpg"
    assert make_poster(video, out)
    assert is_jpeg(out.read_bytes())
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert probe.stdout.strip() == str(posters.POSTER_WIDTH)


def test_a_video_shorter_than_a_second_still_gets_its_first_frame(tmp_path):
    video = real_mp4(tmp_path / "v.mp4", seconds=0.4)
    assert make_poster(video, tmp_path / "p.jpg")


def test_something_that_is_not_a_video_gets_no_poster(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"not a video")
    assert not make_poster(video, tmp_path / "p.jpg")


def test_a_render_stores_the_poster_next_to_the_video(db, cfg, project, cancelled):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)
    outcome = render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
        runner=mp4_runner,
    )  # fmt: skip
    assert outcome == "rendered"
    key = f"output/review/7001/g1/{revision}.jpg"
    assert is_jpeg(repo.store.get_bytes(key, produced_by="t"))


def test_a_render_whose_poster_cannot_be_made_is_still_rendered(
    db, cfg, project, cancelled, monkeypatch
):
    monkeypatch.setattr(posters, "make_poster", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)
    outcome = render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
        runner=mp4_runner,
    )  # fmt: skip
    assert outcome == "rendered" and db.render_row()["state"] == "rendered"
    assert not repo.store.exists(f"output/review/7001/g1/{revision}.jpg")


def test_videos_rendered_before_posters_existed_get_one(db, cfg, project, cancelled, monkeypatch):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)
    monkeypatch.setattr(posters, "make_poster", lambda *a, **k: False)
    render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
        runner=mp4_runner,
    )  # fmt: skip
    monkeypatch.undo()
    key = f"output/review/7001/g1/{revision}.jpg"
    assert not repo.store.exists(key)

    assert backfill(db.worker(), repo.store, threading.Event()) == 1
    assert is_jpeg(repo.store.get_bytes(key, produced_by="t"))
    # Nothing left to make the second time.
    assert backfill(db.worker(), repo.store, threading.Event()) == 0


def test_the_backfill_stops_when_asked_to(db, cfg, project, cancelled, monkeypatch):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)
    monkeypatch.setattr(posters, "make_poster", lambda *a, **k: False)
    render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
        runner=mp4_runner,
    )  # fmt: skip
    monkeypatch.undo()
    stop = threading.Event()
    stop.set()
    assert backfill(db.worker(), repo.store, stop) == 0


class WriteOnlyStore:
    """A store whose reads of `output/` are refused, as the worker's policy refuses them."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def exists(self, key):
        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

    def download_file(self, *args, **kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")


def test_a_worker_that_may_not_read_output_stops_the_backfill_with_one_clear_warning(
    db, cfg, project, cancelled, monkeypatch, caplog
):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)
    monkeypatch.setattr(posters, "make_poster", lambda *a, **k: False)
    render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
        runner=mp4_runner,
    )  # fmt: skip
    monkeypatch.undo()
    with caplog.at_level(logging.WARNING, logger="review_worker.posters"):
        assert backfill(db.worker(), WriteOnlyStore(repo.store), threading.Event()) == 0
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "may not read output/" in warnings[0]
