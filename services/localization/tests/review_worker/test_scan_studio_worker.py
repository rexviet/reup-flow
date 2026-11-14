import dataclasses
import subprocess
import time

import httpx

from localization.review_bridge.service import Service
from localization.review_worker import final_render, heartbeat, review_scan, studio_requests
from localization.review_worker.main import Worker
from tests.review_bridge.test_service import command


def ready_run(db, aweme="7001", generation=1, status="ready_for_review"):
    db.admin.execute(
        "INSERT INTO localization.runs (aweme_id, generation, workflow_id, status, project_key) "
        "VALUES (%s, %s, 'wf', %s, %s)",
        (aweme, generation, status, f"review/{aweme}/g{generation}/head.json"),
    )


def status_row(db):
    return db.admin.execute("SELECT * FROM localization.review_status").fetchone()


# -- scan ---------------------------------------------------------------------


def test_scan_counts_mismatching_cues_and_recognises_export_output(db, project):
    repo, revision = project
    repo.materialize("7001", 1)
    service = Service(repo)
    service.save("7001", 1, 17, command(service, text="Câu khác với lời đã đọc"))
    revision = repo.current("7001", 1)[1].revision_id
    ready_run(db)
    ready_run(db, "7002", 1, status="published")  # not awaiting review: ignored
    folder = repo.folder("7001", 1)
    (folder / "renders").mkdir()
    (folder / "renders" / "g1_2026-11-12_10-00-00.meta.json").write_text("{}")

    assert review_scan.scan(db.worker(), repo) == 1
    row = status_row(db)
    assert (row["revision"], row["cue_count"], row["mismatch_count"], row["dirty"]) == (
        revision, 1, 1, False,
    )  # fmt: skip

    (folder / "index.html").write_text("unsaved Studio edit")
    review_scan.scan(db.worker(), repo)
    assert status_row(db)["dirty"] is True


def test_scan_without_a_local_folder_is_clean_and_creates_nothing(db, project):
    repo, _ = project
    ready_run(db)
    review_scan.scan(db.worker(), repo)
    assert status_row(db)["dirty"] is False
    assert not (repo.root / "7001").exists()


def test_scan_skips_a_broken_project_and_keeps_going(db, project):
    repo, _ = project
    ready_run(db, "7000", 1)  # no HEAD in the bucket
    ready_run(db, "7001", 1)
    assert review_scan.scan(db.worker(), repo) == 1
    assert status_row(db)["aweme_id"] == "7001"


# -- studio requests ----------------------------------------------------------


def request(db, aweme="7001", generation=1):
    db.admin.execute(
        "INSERT INTO studio_open_requests (aweme_id, generation) VALUES (%s, %s)",
        (aweme, generation),
    )


def states(db):
    return [
        (r["state"], r["error"])
        for r in db.admin.execute("SELECT * FROM studio_open_requests ORDER BY id")
    ]


def test_pending_request_becomes_done(db, cfg):
    request(db)
    seen = []
    n = studio_requests.handle_requests(
        db.worker(), cfg, {}, runner=lambda c, a, g, e: seen.append((a, g))
    )
    assert n == 1 and seen == [("7001", 1)] and states(db) == [("done", None)]
    assert studio_requests.handle_requests(db.worker(), cfg, {}, runner=seen.append) == 0


def test_failed_request_records_the_reason(db, cfg):
    request(db)
    request(db, "7002")

    def runner(cfg, aweme, generation, env):
        if aweme == "7001":
            raise RuntimeError("Studio ownership changed")
        raise subprocess.TimeoutExpired("helper", 1)

    studio_requests.handle_requests(db.worker(), cfg, {}, runner=runner)
    first, second = states(db)
    assert first == ("failed", "Studio ownership changed")
    assert second[0] == "failed" and "quá thời gian" in second[1]
    handled = db.admin.execute("SELECT handled_at FROM studio_open_requests").fetchall()
    assert all(r["handled_at"] is not None for r in handled)


def test_helper_rejects_bad_identity_and_reports_stderr(cfg, tmp_path):
    script = tmp_path / "helper.mjs"
    script.write_text("console.error('bad things'); process.exit(2)")
    config = dataclasses.replace(cfg, studio_script=script, node="node")
    try:
        studio_requests.run_helper(config, "7001; rm", 1, {})
    except ValueError:
        pass
    else:
        raise AssertionError("identity must be validated")
    try:
        studio_requests.run_helper(config, "7001", 1, {"PATH": "/usr/local/bin:/usr/bin:/bin"})
    except RuntimeError as exc:
        assert "bad things" in str(exc)
    else:
        raise AssertionError("non-zero exit must fail")


# -- heartbeat and loop -------------------------------------------------------


def test_voicestudio_probe_reports_up_and_down():
    up = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))

    def refuse(request):
        raise httpx.ConnectError("refused")

    gone = httpx.Client(transport=httpx.MockTransport(refuse))
    assert heartbeat.probe_voicestudio("http://127.0.0.1:3900", client=up)["ok"] is True
    assert heartbeat.probe_voicestudio("http://127.0.0.1:3900", client=down)["ok"] is False
    assert heartbeat.probe_voicestudio("http://127.0.0.1:3900", client=gone)["ok"] is False
    assert heartbeat.probe_voicestudio(None)["ok"] is False


def host_info(db):
    return db.admin.execute("SELECT * FROM localization.host_workers").fetchone()["info"]


def make_worker(db, cfg, store, **kwargs):
    kwargs.setdefault("probe", lambda: {"ok": True, "latency_ms": 3, "checked_at": "now"})
    return Worker(cfg, store, connector=lambda _cfg: db.worker(), **kwargs)


def test_low_disk_pauses_claiming_and_says_why_in_the_heartbeat(db, cfg, project):
    repo, revision = project
    db.approve("7001", 1, revision)
    worker = make_worker(db, dataclasses.replace(cfg, min_free_gb=10**9), repo.store)
    worker.tick()
    info = host_info(db)
    assert db.render_row()["state"] == "queued"
    assert "Đĩa còn dưới" in info["paused_reason"]
    assert info["disk_free_bytes"] > 0 and info["disk_total_bytes"] >= info["disk_free_bytes"]
    assert info["voicestudio"]["ok"] is True and info["current"] is None


def test_loop_claims_and_renders_one_row_at_a_time(db, cfg, project):
    repo, revision = project
    db.approve("7001", 1, revision)
    db.approve("7002", 1, revision)
    rendered = []

    def render(row, **kwargs):
        rendered.append(row["aweme_id"])
        return "rendered"

    worker = make_worker(db, cfg, repo.store, render=render)
    worker.tick()
    assert worker.current["aweme_id"] in {"7001", "7002"}
    assert host_info(db)["current"] is None  # the beat precedes the claim
    worker.thread.join(5)
    worker.tick()
    worker.thread.join(5)
    assert sorted(rendered) == ["7001", "7002"]
    worker.tick()
    assert host_info(db)["current"] is None


def test_shutdown_gives_the_running_render_back_to_the_queue(db, cfg, project):
    repo, revision = project
    db.approve("7001", 1, revision)
    started = []

    def render(row, *, conn, cancelled, **kwargs):
        started.append(1)
        cancelled.wait(10)
        conn.execute(
            final_render.REQUEUE_SQL,
            {"aweme": row["aweme_id"], "gen": row["generation"], "claim": row["claim"]},
        )
        return "queued"

    worker = make_worker(
        db, dataclasses.replace(cfg, shutdown_grace_s=0), repo.store, render=render
    )
    worker.tick()
    while not started:
        time.sleep(0.01)
    worker.shutdown()
    after = db.render_row()
    assert after["state"] == "queued" and after["attempts"] == 0 and after["claim"] is None
    assert not worker.rendering()


def test_render_role_renders_under_its_own_name_and_leaves_studio_work_alone(
    db, cfg, project, monkeypatch
):
    repo, revision = project
    db.approve("7001", 1, revision)
    ready_run(db)
    scanned = []
    monkeypatch.setattr(review_scan, "scan", lambda *a: scanned.append(1))
    rendered = []
    worker = make_worker(
        db, dataclasses.replace(cfg, roles=frozenset({"render"}), name="render-worker-win"),
        repo.store, render=lambda row, **kw: rendered.append(row["aweme_id"]) or "rendered",
    )  # fmt: skip
    worker.tick()
    worker.thread.join(5)
    assert rendered == ["7001"] and scanned == [] and worker.studio_thread is None
    names = [r["name"] for r in db.admin.execute("SELECT name FROM localization.host_workers")]
    assert names == ["render-worker-win"]


def test_studio_role_never_claims_a_render(db, cfg, project, monkeypatch):
    repo, revision = project
    db.approve("7001", 1, revision)
    scanned = []
    monkeypatch.setattr(review_scan, "scan", lambda *a: scanned.append(1))
    worker = make_worker(db, dataclasses.replace(cfg, roles=frozenset({"studio"})), repo.store)
    worker.tick()
    worker.studio_thread.join(5)
    assert worker.thread is None and db.render_row()["state"] == "queued"
    assert scanned == [1]
    assert db.admin.execute("SELECT name FROM localization.host_workers").fetchone()["name"] == (
        "review-worker"
    )


def test_claim_and_finish_are_logged(db, cfg, project, caplog):
    repo, revision = project
    db.approve("7001", 1, revision)
    worker = make_worker(db, cfg, repo.store, render=lambda row, **kw: "rendered")
    with caplog.at_level("INFO", logger="review_worker"):
        worker.tick()
        worker.thread.join(5)
    messages = [r.getMessage() for r in caplog.records]
    assert "render 7001/g1: started (attempt 1)" in messages
    assert any(m.startswith("render 7001/g1: rendered in 0m0") for m in messages)
