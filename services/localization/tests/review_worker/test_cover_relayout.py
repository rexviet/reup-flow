"""A collection's new cover box reaches the videos still awaiting review: a real review
project in a mock bucket, a scratch database read as the `localization` role."""

import dataclasses
import logging

import pytest
from psycopg.rows import dict_row, tuple_row

from localization.review_bridge.repository import Conflict
from localization.review_worker import cover_relayout, review_scan
from tests.review_worker.conftest import CORE, DELIVERY, PROJECT_KEY, Db
from tests.review_worker.test_scan_studio_worker import make_worker, ready_run
from tests.starter.test_grants import GRANTS
from tests.support.scratch_db import (
    COLLECTIONS,
    DASHBOARD_INTAKE,
    INIT,
    OUTBOX,
    add_channel_collection,
    scratch_database,
)

WIDE = {"cover_offset_pct": 3, "cover_width_pct": 100}
WIDE_BOX = ".caption,.source-mask{position:absolute;left:0%;width:100%;"


@pytest.fixture
def db():
    for info, admin in scratch_database(
        INIT, OUTBOX, DASHBOARD_INTAKE, CORE, PROJECT_KEY, DELIVERY, GRANTS, COLLECTIONS
    ):
        admin.row_factory = dict_row
        state = Db(info, admin)
        yield state
        for conn in state.opened:
            conn.close()


def awaiting(db, aweme="7001", generation=1, status="ready_for_review", **geometry) -> str:
    """A video of the channel's collection, with these cover settings, whose run waits for
    review. Returns the collection id."""
    db.admin.row_factory = tuple_row  # the helper reads rows by position
    try:
        collection = add_channel_collection(db.admin, "chan")
    finally:
        db.admin.row_factory = dict_row
    if geometry:
        set_geometry(db, collection, **geometry)
    db.admin.execute(
        "INSERT INTO videos (aweme_id, sec_user_id, create_time, status, collection_id) "
        "VALUES (%s, 'chan', now(), 'done', %s) ON CONFLICT DO NOTHING",
        (aweme, collection),
    )
    ready_run(db, aweme, generation, status)
    return collection


def set_geometry(db, collection, cover_offset_pct=None, cover_width_pct=None,
                 cover_height_pct=None) -> None:  # fmt: skip
    db.admin.execute(
        "UPDATE collections SET cover_offset_pct = %s, cover_width_pct = %s, "
        "cover_height_pct = %s WHERE id = %s",
        (cover_offset_pct, cover_width_pct, cover_height_pct, collection),
    )


def head(repo):
    return repo.current("7001", 1)[1]


def test_a_changed_box_gives_the_waiting_video_a_new_head_once(db, project, caplog) -> None:
    repo, revision = project
    awaiting(db, **WIDE)
    before = head(repo)

    with caplog.at_level(logging.INFO, logger="review_worker.cover_relayout"):
        assert cover_relayout.relayout(db.worker(), repo) == 1
    after = head(repo)
    assert after.revision_id != revision
    assert (after.parent_revision_id, after.operation_id) == (revision, "cover-relayout")
    assert (after.cover_offset_pct, after.cover_width_pct, after.cover_height_pct) == (3, 100, None)
    assert after.cues == before.cues and after.mixed_audio == before.mixed_audio
    html = repo.read_asset(after.html).decode()
    assert WIDE_BOX in html and ".caption{top:301px;height:40px;" in html
    assert [r.message for r in caplog.records] == [
        f"cover relayout 7001/g1: {revision} -> {after.revision_id} "
        "(offset 3, width 100, height None)"
    ]

    # The next scan finds HEAD already drawn with the collection's box.
    assert cover_relayout.relayout(db.worker(), repo) == 0
    assert head(repo).revision_id == after.revision_id


def test_a_box_set_back_to_automatic_is_drawn_as_before(db, project) -> None:
    repo, revision = project
    collection = awaiting(db, **WIDE)
    cover_relayout.relayout(db.worker(), repo)
    set_geometry(db, collection)
    assert cover_relayout.relayout(db.worker(), repo) == 1
    after = head(repo)
    assert (after.cover_offset_pct, after.cover_width_pct) == (None, None)
    original = repo.manifest("7001", 1, revision)
    assert repo.read_asset(after.html) == repo.read_asset(original.html)


def test_an_unchanged_box_makes_no_revision(db, project) -> None:
    repo, revision = project
    awaiting(db)  # the collection sets nothing; neither did the run
    assert cover_relayout.relayout(db.worker(), repo) == 0
    assert head(repo).revision_id == revision


def test_studio_visual_edits_are_kept(db, project) -> None:
    repo, _ = project
    _, base, etag = repo.current("7001", 1)
    styled = repo.prepare(base, base.cues, {"cue-17": {"color": "#ff0000"}})
    repo.activate(styled, etag)
    awaiting(db, status="needs_human", **WIDE)
    assert cover_relayout.relayout(db.worker(), repo) == 1
    html = repo.read_asset(head(repo).html).decode()
    assert 'id="cue-17" style="color:#ff0000" class=' in html and WIDE_BOX in html


@pytest.mark.parametrize("decided", ["approved", "returned"])
def test_a_video_with_a_decision_is_left_alone(db, project, decided) -> None:
    repo, revision = project
    awaiting(db, **WIDE)
    db.admin.execute(
        "INSERT INTO review_decisions (aweme_id, generation, decision, approved_revision) "
        "VALUES ('7001', 1, %s, %s)",
        (decided, revision if decided == "approved" else None),
    )
    assert cover_relayout.relayout(db.worker(), repo) == 0
    assert head(repo).revision_id == revision


def test_a_queued_or_rendered_video_is_left_alone(db, project) -> None:
    repo, revision = project
    awaiting(db, **WIDE)
    db.queue("7001", 1, revision)
    assert cover_relayout.relayout(db.worker(), repo) == 0
    db.admin.execute("UPDATE localization.final_renders SET state = 'rendered'")
    assert cover_relayout.relayout(db.worker(), repo) == 0
    assert head(repo).revision_id == revision


def test_a_video_no_longer_awaiting_review_is_left_alone(db, project) -> None:
    repo, revision = project
    awaiting(db, status="published", **WIDE)
    assert cover_relayout.relayout(db.worker(), repo) == 0
    assert head(repo).revision_id == revision


def test_unsaved_studio_edits_hold_the_relayout_until_the_folder_is_clean(db, project) -> None:
    repo, revision = project
    awaiting(db, **WIDE)
    repo.materialize("7001", 1)
    index = repo.folder("7001", 1) / "index.html"
    saved = index.read_bytes()
    index.write_text("unsaved Studio edit")
    assert cover_relayout.relayout(db.worker(), repo) == 0
    assert head(repo).revision_id == revision
    index.write_bytes(saved)
    assert cover_relayout.relayout(db.worker(), repo) == 1


def test_a_head_that_moved_meanwhile_wins_until_the_next_scan(db, project, monkeypatch) -> None:
    repo, revision = project
    awaiting(db, **WIDE)

    def lost(candidate, etag):
        raise Conflict("f" * 32)

    with monkeypatch.context() as patch:
        patch.setattr(repo, "activate", lost)
        assert cover_relayout.relayout(db.worker(), repo) == 0
    assert head(repo).revision_id == revision
    assert cover_relayout.relayout(db.worker(), repo) == 1


def test_a_broken_project_never_stops_the_rest(db, project, caplog) -> None:
    repo, revision = project
    awaiting(db, "7000", **WIDE)  # a run whose project was never written
    awaiting(db, **WIDE)
    assert cover_relayout.relayout(db.worker(), repo) == 1
    assert head(repo).revision_id != revision
    assert "cover relayout of 7000/g1 failed: ArtifactMissingError" in caplog.text


def test_a_failed_queue_read_is_logged_not_raised(db, project, caplog) -> None:
    repo, _ = project
    awaiting(db, **WIDE)
    # Stands in for any failed read, a database without the new columns say.
    db.admin.execute("REVOKE SELECT ON collections FROM localization")
    assert cover_relayout.relayout(db.worker(), repo) == 0
    assert "cover relayout skipped: InsufficientPrivilege" in caplog.text


def test_the_studio_loop_relays_out_before_it_scans(db, cfg, project, monkeypatch) -> None:
    repo, _ = project
    calls = []
    monkeypatch.setattr(cover_relayout, "relayout", lambda *a: calls.append("relayout"))
    monkeypatch.setattr(review_scan, "scan", lambda *a: calls.append("scan"))
    worker = make_worker(db, dataclasses.replace(cfg, roles=frozenset({"studio"})), repo.store)
    worker.tick()
    worker.studio_thread.join(5)
    assert calls == ["relayout", "scan"]
