import os
import sys
import threading
import time
from pathlib import Path

import pytest

from localization.review_worker import final_render
from localization.review_worker.final_render import (
    Cancelled,
    ClaimLost,
    claim,
    render_claimed,
    run_cli,
)

REV = "a" * 32


def ok_runner(content=b"mp4-bytes"):
    def runner(command, *, cwd, env, log_path, **_):
        Path(command[command.index("-o") + 1]).write_bytes(content)

    return runner


def test_two_workers_never_claim_the_same_row(db, cfg):
    db.approve("7001")
    db.approve("7002")
    one, two = db.worker(), db.worker()
    first, second, third = claim(one, cfg), claim(two, cfg), claim(one, cfg)
    assert {first["aweme_id"], second["aweme_id"]} == {"7001", "7002"}
    assert third is None
    assert first["claim"] != second["claim"] and first["attempts"] == 1


def test_a_row_without_a_matching_approval_is_never_claimed(db, cfg):
    db.queue("7001", 1, REV)  # no decision at all
    db.queue("7002", 1, REV)
    db.admin.execute(
        "INSERT INTO review_decisions (aweme_id, generation, decision) "
        "VALUES ('7002', 1, 'returned')"
    )
    db.approve("7003", 1, "b" * 32, queued=False)
    db.queue("7003", 1, "c" * 32)  # approved a different revision
    assert claim(db.worker(), cfg) is None
    db.approve("7001", 1, REV, queued=False)
    assert claim(db.worker(), cfg)["aweme_id"] == "7001"


def test_expired_lease_is_claimed_again_but_only_three_times(db, cfg):
    db.approve()
    conn = db.worker()
    for attempt in (1, 2, 3):
        row = claim(conn, cfg)
        assert row["attempts"] == attempt
        db.admin.execute(
            "UPDATE localization.final_renders SET lease_expires_at = now() - interval '1 min'"
        )
    assert claim(conn, cfg) is None
    stuck = db.render_row()
    assert stuck["state"] == "failed" and "3" in stuck["error"] and stuck["claim"] is None


def test_live_lease_is_not_claimed(db, cfg):
    db.approve()
    conn = db.worker()
    assert claim(conn, cfg) is not None
    assert claim(db.worker(), cfg) is None


def test_result_of_a_stale_claim_cannot_overwrite(db, cfg, project, cancelled):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)

    def reclaimed(command, **kwargs):
        ok_runner()(command, **kwargs)
        db.admin.execute("UPDATE localization.final_renders SET claim = gen_random_uuid()")

    outcome = render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
        runner=reclaimed,
    )  # fmt: skip
    after = db.render_row()
    assert outcome == "dropped" and after["state"] == "rendering" and after["final_key"] is None


def test_success_uploads_mp4_and_srt_for_the_approved_revision(db, cfg, project, cancelled):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)
    folder_seen = []

    def runner(command, *, cwd, env, **kwargs):
        folder_seen.append(cwd)
        assert command[:3] == ["node", "/bin/hyperframes.mjs", "render"]
        assert command[command.index("-q") + 1] == "delivery"
        assert (cwd / "index.html").exists() and (cwd / "captions.srt").exists()
        assert not any(key.endswith(("SECRET", "KEY", "URL")) for key in env)
        ok_runner()(command, cwd=cwd, env=env, **kwargs)

    outcome = render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled, runner=runner
    )
    after = db.render_row()
    base = f"output/review/7001/g1/{revision}"
    assert outcome == "rendered" and after["state"] == "rendered"
    assert (after["final_key"], after["srt_key"]) == (base + ".mp4", base + ".srt")
    assert after["rendered_at"] is not None and after["claim"] is None
    assert repo.store.get_bytes(base + ".mp4", produced_by="t") == b"mp4-bytes"
    expected = repo.read_asset(repo.manifest("7001", 1, revision).srt)
    assert repo.store.get_bytes(base + ".srt", produced_by="t") == expected
    assert not folder_seen[0].exists()


def test_head_changed_after_approval_fails_without_rendering(db, cfg, project, cancelled):
    repo, revision = project
    db.approve("7001", 1, "f" * 32)  # approval refers to a revision that is not HEAD
    conn = db.worker()
    row = claim(conn, cfg)
    outcome = render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
        runner=lambda *a, **k: pytest.fail("must not render"),
    )  # fmt: skip
    after = db.render_row()
    assert outcome == "failed" and after["state"] == "failed"
    assert "duyệt lại bản mới" in after["error"]
    assert revision != "f" * 32


def test_returned_decision_drops_the_result(db, cfg, project, cancelled):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)

    def returned(command, **kwargs):
        ok_runner()(command, **kwargs)
        db.admin.execute(
            "UPDATE review_decisions SET decision = 'returned', approved_revision = NULL"
        )

    outcome = render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
        runner=returned,
    )  # fmt: skip
    after = db.render_row()
    assert outcome == "dropped" and after["state"] == "failed" and after["final_key"] is None


def test_temp_directory_is_removed_when_the_render_fails(db, cfg, project, cancelled):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)

    def boom(command, **kwargs):
        raise RuntimeError("chrome crashed")

    outcome = render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled, runner=boom
    )
    after = db.render_row()
    assert outcome == "queued" and after["state"] == "queued" and "chrome crashed" in after["error"]
    assert list(cfg.work_root.iterdir()) == []


def test_third_failed_attempt_stops_for_good(db, cfg, project, cancelled):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()

    def boom(command, **kwargs):
        raise RuntimeError("no luck")

    outcomes = []
    for _ in range(4):
        row = claim(conn, cfg)
        if row is None:
            break
        outcomes.append(
            render_claimed(
                row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
                runner=boom,
            )
        )  # fmt: skip
    assert outcomes == ["queued", "queued", "failed"]
    assert db.render_row()["state"] == "failed"


def test_cancel_returns_the_row_to_the_queue_and_gives_the_attempt_back(
    db, cfg, project, cancelled
):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)

    def interrupted(command, **kwargs):
        raise Cancelled()

    outcome = render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled,
        runner=interrupted,
    )  # fmt: skip
    after = db.render_row()
    assert outcome == "queued" and after["state"] == "queued" and after["attempts"] == 0
    assert after["claim"] is None
    assert list(cfg.work_root.iterdir()) == []


def cli(script: str, tmp_path, **kwargs):
    params = dict(
        cwd=tmp_path, env={"PATH": os.environ["PATH"]}, log_path=tmp_path / "log",
        timeout_s=30, renew_every_s=3600, renew=lambda: True, cancelled=threading.Event(),
    )  # fmt: skip
    params.update(kwargs)
    run_cli([sys.executable, "-c", script], **params)


SPAWNS_CHILD = (
    "import subprocess,sys,time;"
    "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
    "print(c.pid,flush=True);time.sleep(60)"
)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_kills_the_whole_process_group(tmp_path):
    with pytest.raises(TimeoutError):
        cli(SPAWNS_CHILD, tmp_path, timeout_s=2)
    child = int((tmp_path / "log").read_text().split()[0])
    time.sleep(0.5)
    assert not alive(child)


def test_lost_lease_and_cancellation_stop_the_render(tmp_path):
    with pytest.raises(ClaimLost):
        cli("import time;time.sleep(60)", tmp_path, renew_every_s=0, renew=lambda: False)
    event = threading.Event()
    threading.Timer(1, event.set).start()
    with pytest.raises(Cancelled):
        cli("import time;time.sleep(60)", tmp_path, cancelled=event)


def test_non_zero_exit_reports_the_log_tail(tmp_path):
    with pytest.raises(RuntimeError, match="mã 3.*boom"):
        cli("import sys;print('boom');sys.exit(3)", tmp_path)


def test_disk_threshold(tmp_path):
    assert final_render.disk_ok(tmp_path, 0)[0] is True
    assert final_render.disk_ok(tmp_path, 10**9)[0] is False


def test_cancel_before_the_upload_requeues_instead_of_uploading(db, cfg, project, cancelled):
    repo, revision = project
    db.approve("7001", 1, revision)
    conn = db.worker()
    row = claim(conn, cfg)

    def runner(command, **kwargs):
        ok_runner()(command, **kwargs)
        cancelled.set()

    outcome = render_claimed(
        row, conn=conn, store=repo.store, repo=repo, cfg=cfg, cancelled=cancelled, runner=runner
    )
    after = db.render_row()
    assert outcome == "queued" and after["state"] == "queued" and after["attempts"] == 0
    assert not repo.store.exists(f"output/review/7001/g1/{revision}.mp4")


def test_session_reopens_a_dropped_connection_once():
    import psycopg

    class Conn:
        def __init__(self, fail):
            self.fail, self.closed, self.broken = fail, False, False

        def close(self):
            self.closed = True

        def execute(self, sql, params=None):
            if self.fail:
                raise psycopg.OperationalError("server closed the connection")
            return sql

    made = []

    def connect():
        made.append(Conn(fail=len(made) == 0))
        return made[-1]

    session = final_render.Session(connect)
    assert session.execute("select 1") == "select 1"
    assert len(made) == 2

    def always_down():
        return Conn(fail=True)

    with pytest.raises(psycopg.OperationalError):
        final_render.Session(always_down).execute("select 1")


def test_children_left_behind_by_a_finished_renderer_are_killed(tmp_path):
    script = (
        "import subprocess,sys;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(c.pid,flush=True)"
    )
    cli(script, tmp_path)
    child = int((tmp_path / "log").read_text().split()[0])
    time.sleep(0.3)
    assert not alive(child)
