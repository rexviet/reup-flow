"""A collection's watermark images reach the videos already made: a real review project in a
mock bucket, a scratch database read as the `localization` role, the search stubbed."""

import threading

import pytest
from psycopg.rows import dict_row

from localization.contracts import BBox, Watermark
from localization.review_bridge.repository import Conflict
from localization.review_worker.watermark_relayout import OPERATION, WatermarkRelayout
from tests.review_worker.conftest import CORE, DELIVERY, PROJECT_KEY, Db
from tests.review_worker.test_cover_relayout import awaiting
from tests.starter.test_grants import GRANTS
from tests.support.scratch_db import (
    COLLECTIONS,
    DASHBOARD_INTAKE,
    INIT,
    OUTBOX,
    WATERMARKS,
    scratch_database,
)

MARK = Watermark(region=BBox(x=16, y=30, w=150, h=30), start_ms=0, end_ms=1000)
BLUR = 'class="clip watermark-mask"'


@pytest.fixture
def db():
    for info, admin in scratch_database(
        INIT, OUTBOX, DASHBOARD_INTAKE, CORE, PROJECT_KEY, DELIVERY, GRANTS, COLLECTIONS,
        WATERMARKS,
    ):  # fmt: skip
        admin.row_factory = dict_row
        state = Db(info, admin)
        yield state
        for conn in state.opened:
            conn.close()


class Finder:
    """Stands in for the search: records what it was asked and finds `MARK`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, project, templates, cancel):
        self.calls.append((project.aweme_id, tuple(t.sha256 for t in templates)))
        return (MARK,)


def add_image(db, collection: str, n: int = 1) -> str:
    sha = f"{n:x}" * 64
    db.admin.execute(
        "INSERT INTO collection_watermarks (collection_id, image_key, image_sha256, "
        "frame_width, frame_height, aweme_id, time_ms, x, y, w, h) "
        "VALUES (%s, %s, %s, 360, 640, '7001', 0, 0, 0, 100, 40)",
        (collection, f"source/collections/{collection}/watermarks/{n}.png", sha),
    )
    return sha


def head(repo, aweme="7001"):
    return repo.current(aweme, 1)[1]


def relayout(repo, finder=None):
    return WatermarkRelayout(repo, repo.store, repo.root / "marks", finder=finder or Finder())


def run(db, relay) -> int:
    return relay.run_pass(db.worker(), threading.Event())


def test_a_waiting_video_gets_the_blur_its_collections_images_find(db, project) -> None:
    repo, revision = project
    sha = add_image(db, awaiting(db))
    finder = Finder()
    relay = relayout(repo, finder)
    assert run(db, relay) == 1
    after = head(repo)
    assert (after.parent_revision_id, after.operation_id) == (revision, OPERATION)
    assert after.watermarks == (MARK,) and after.watermark_templates == (sha,)
    assert after.cues == repo.manifest("7001", 1, revision).cues
    assert BLUR in repo.read_asset(after.html).decode()
    assert finder.calls == [("7001", (sha,))]
    # Done: neither this pass object nor a fresh one (a restarted worker) searches again.
    assert run(db, relay) == 0 and run(db, relayout(repo, finder)) == 0
    assert len(finder.calls) == 1


def test_a_collection_without_images_leaves_its_videos_as_they_are(db, project) -> None:
    repo, revision = project
    awaiting(db)
    finder = Finder()
    assert run(db, relayout(repo, finder)) == 0
    assert head(repo).revision_id == revision and finder.calls == []


def test_removing_every_image_removes_the_blur_without_a_search(db, project) -> None:
    repo, _ = project
    add_image(db, awaiting(db))
    relay = relayout(repo)
    run(db, relay)
    db.admin.execute("DELETE FROM collection_watermarks")
    finder = Finder()
    relay.finder = finder
    assert run(db, relay) == 1
    after = head(repo)
    assert after.watermarks == () and after.watermark_templates == ()
    assert BLUR not in repo.read_asset(after.html).decode() and finder.calls == []


def test_one_search_a_pass(db, project, review_store, tmp_path) -> None:
    repo, _ = project
    collection = awaiting(db)
    add_image(db, collection)
    awaiting(db, "7002")  # same channel, same collection; a project that was never written
    finder = Finder()
    relay = relayout(repo, finder)
    assert run(db, relay) == 1
    assert finder.calls == [("7001", (f"{1:x}" * 64,))]


def test_studio_visual_edits_are_kept(db, project) -> None:
    repo, _ = project
    _, base, etag = repo.current("7001", 1)
    repo.activate(repo.prepare(base, base.cues, {"cue-17": {"color": "#ff0000"}}), etag)
    add_image(db, awaiting(db))
    assert run(db, relayout(repo)) == 1
    html = repo.read_asset(head(repo).html).decode()
    assert 'id="cue-17" style="color:#ff0000" class=' in html and BLUR in html


def test_unsaved_studio_edits_hold_it_without_a_search(db, project) -> None:
    repo, revision = project
    add_image(db, awaiting(db))
    repo.materialize("7001", 1)
    index = repo.folder("7001", 1) / "index.html"
    saved = index.read_bytes()
    index.write_text("unsaved Studio edit")
    finder = Finder()
    relay = relayout(repo, finder)
    assert run(db, relay) == 0
    assert head(repo).revision_id == revision and finder.calls == []
    index.write_bytes(saved)
    assert run(db, relay) == 1


def test_a_head_that_moved_during_the_search_wins_until_the_next_pass(
    db, project, monkeypatch
) -> None:
    repo, revision = project
    add_image(db, awaiting(db))
    relay = relayout(repo)

    def lost(candidate, etag):
        raise Conflict("f" * 32)

    with monkeypatch.context() as patch:
        patch.setattr(repo, "activate", lost)
        assert run(db, relay) == 0
    assert head(repo).revision_id == revision
    assert run(db, relay) == 1


def test_a_cancelled_search_commits_nothing(db, project) -> None:
    repo, revision = project
    add_image(db, awaiting(db))
    stopping = threading.Event()

    def stopped(project, templates, cancel):
        stopping.set()
        return ()

    assert relayout(repo, stopped).run_pass(db.worker(), stopping) == 0
    assert head(repo).revision_id == revision


def test_a_returned_video_is_left_alone(db, project) -> None:
    repo, revision = project
    add_image(db, awaiting(db))
    db.admin.execute(
        "INSERT INTO review_decisions (aweme_id, generation, decision) "
        "VALUES ('7001', 1, 'returned')"
    )
    finder = Finder()
    assert run(db, relayout(repo, finder)) == 0
    assert head(repo).revision_id == revision and finder.calls == []


def test_an_approved_video_moves_its_approval_and_is_rendered_again(db, project) -> None:
    repo, revision = project
    sha = add_image(db, awaiting(db))
    db.approve("7001", 1, revision)
    db.admin.execute(
        "UPDATE localization.final_renders SET state = 'rendered', attempts = 1, "
        "final_key = 'review/7001/g1/final/old.mp4'"
    )
    assert run(db, relayout(repo)) == 1
    after = head(repo)
    assert after.parent_revision_id == revision and after.watermark_templates == (sha,)
    decision = db.admin.execute("SELECT * FROM review_decisions").fetchone()
    assert (decision["decision"], decision["approved_revision"]) == ("approved", after.revision_id)
    render = db.render_row()
    assert (render["state"], render["attempts"], render["approved_revision"]) == (
        "queued", 0, after.revision_id,
    )  # fmt: skip


def test_an_approved_video_being_rendered_waits(db, project) -> None:
    repo, revision = project
    add_image(db, awaiting(db))
    db.approve("7001", 1, revision)
    db.admin.execute("UPDATE localization.final_renders SET state = 'rendering'")
    finder = Finder()
    relay = relayout(repo, finder)
    assert run(db, relay) == 0
    assert head(repo).revision_id == revision and finder.calls == []
    db.admin.execute("UPDATE localization.final_renders SET state = 'rendered'")
    assert run(db, relay) == 1
    assert db.render_row()["state"] == "queued"


def test_an_approval_withdrawn_during_the_search_keeps_head_and_decision(db, project) -> None:
    repo, revision = project
    add_image(db, awaiting(db))
    db.approve("7001", 1, revision)

    def returned_meanwhile(project, templates, cancel):
        db.admin.execute("UPDATE review_decisions SET decision = 'returned'")
        return (MARK,)

    assert run(db, relayout(repo, returned_meanwhile)) == 0
    assert head(repo).revision_id == revision
    assert db.render_row()["approved_revision"] == revision


def test_an_approval_move_that_did_not_commit_is_finished_by_the_next_pass(
    db, project, monkeypatch
) -> None:
    repo, revision = project
    add_image(db, awaiting(db))
    db.approve("7001", 1, revision)
    relay = relayout(repo)
    # HEAD moves to the new revision, then the database write fails.
    real = repo.activate

    def then_fail(candidate, etag):
        real(candidate, etag)
        raise RuntimeError("connection lost")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "activate", then_fail)
        assert run(db, relay) == 0
    new = head(repo)
    assert new.parent_revision_id == revision
    assert db.render_row()["approved_revision"] == revision
    finder = Finder()
    relay.finder = finder
    assert run(db, relay) == 1
    assert db.render_row()["approved_revision"] == new.revision_id
    assert head(repo).revision_id == new.revision_id and finder.calls == []


def test_an_approved_video_edited_after_its_approval_is_left_alone(db, project) -> None:
    repo, revision = project
    add_image(db, awaiting(db))
    db.approve("7001", 1, revision)
    _, base, etag = repo.current("7001", 1)
    edited = repo.prepare(base, base.cues, {"cue-17": {"color": "#ff0000"}})
    repo.activate(edited, etag)
    finder = Finder()
    assert run(db, relayout(repo, finder)) == 0
    assert head(repo).revision_id == edited.revision_id and finder.calls == []
    assert db.render_row()["approved_revision"] == revision


def test_an_approved_video_whose_blur_came_out_as_it_was_keeps_its_render(db, project) -> None:
    repo, revision = project
    add_image(db, awaiting(db))
    db.approve("7001", 1, revision)
    db.admin.execute("UPDATE localization.final_renders SET state = 'rendered', attempts = 1")
    assert run(db, relayout(repo, lambda project, templates, cancel: ())) == 1
    after = head(repo)
    render = db.render_row()
    assert (render["state"], render["attempts"], render["approved_revision"]) == (
        "rendered", 1, after.revision_id,
    )  # fmt: skip
    decision = db.admin.execute("SELECT approved_revision FROM review_decisions").fetchone()
    assert decision["approved_revision"] == after.revision_id


def test_a_project_found_by_an_earlier_search_is_found_again(db, project) -> None:
    repo, _ = project
    sha = add_image(db, awaiting(db))
    _, base, etag = repo.current("7001", 1)
    earlier = repo.prepare(
        base, base.cues, {}, watermarks=(MARK,), watermark_templates=(sha,), watermark_method=1
    )
    repo.activate(earlier, etag)
    finder = Finder()
    assert run(db, relayout(repo, finder)) == 1
    assert (
        finder.calls == [("7001", (sha,))] and head(repo).parent_revision_id == earlier.revision_id
    )
