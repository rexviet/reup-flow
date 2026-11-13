"""Held videos run one at a time: the end of one run releases the next, in the database."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from tests.support.scratch_db import DASHBOARD_INTAKE, INIT, OUTBOX, scratch_database

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
CORE = (MIGRATIONS / "261026-localization-core.sql").read_text()
QUEUE = (MIGRATIONS / "261113-sequential-queue.sql").read_text()
QUEUE_DOWN = (MIGRATIONS / "261113-sequential-queue.down.sql").read_text()


@pytest.fixture
def db() -> Iterator[psycopg.Connection]:
    for _, conn in scratch_database(INIT, OUTBOX, DASHBOARD_INTAKE, CORE, QUEUE):
        conn.execute("INSERT INTO profiles (sec_user_id) VALUES ('c')")
        yield conn


def add_video(db: psycopg.Connection, aweme: str, state: str = "held") -> None:
    db.execute(
        "INSERT INTO videos (aweme_id, sec_user_id, create_time, storage_path, status) "
        "VALUES (%s, 'c', now(), 's3://b/source/x.mp4', 'done')",
        (aweme,),
    )
    db.execute("INSERT INTO localization_outbox (aweme_id, state) VALUES (%s, %s)", (aweme, state))


def states(db: psycopg.Connection) -> dict[str, str]:
    rows = db.execute(
        "SELECT aweme_id, state FROM localization_outbox ORDER BY aweme_id"
    ).fetchall()
    return {aweme: state for aweme, state in rows}


def write_run(db: psycopg.Connection, aweme: str, status: str = "ready_for_review") -> None:
    """The `publish` activity writes this as the localization role."""
    db.execute("SET ROLE localization")
    try:
        db.execute(
            "INSERT INTO localization.runs (aweme_id, generation, workflow_id, status) "
            "VALUES (%s, 1, 'w', %s) ON CONFLICT (aweme_id, generation) "
            "DO UPDATE SET status = EXCLUDED.status",
            (aweme, status),
        )
    finally:
        db.execute("RESET ROLE")


def test_the_first_held_video_starts_by_itself_and_the_rest_wait(db: psycopg.Connection) -> None:
    add_video(db, "9")  # a later upload
    assert states(db) == {"9": "pending"}
    add_video(db, "10")
    add_video(db, "8")
    assert states(db) == {"10": "held", "8": "held", "9": "pending"}


def test_a_finished_run_releases_the_oldest_upload_next(db: psycopg.Connection) -> None:
    add_video(db, "5")
    add_video(db, "30")
    add_video(db, "100")
    add_video(db, "20")
    write_run(db, "5")
    assert states(db) == {"100": "held", "20": "pending", "30": "held", "5": "pending"}
    write_run(db, "20")
    assert states(db)["30"] == "pending" and states(db)["100"] == "held"


def test_a_failed_run_still_releases_the_next(db: psycopg.Connection) -> None:
    add_video(db, "1")
    add_video(db, "2")
    write_run(db, "1", "failed")
    assert states(db)["2"] == "pending"


def test_writing_the_same_run_again_releases_nothing_more(db: psycopg.Connection) -> None:
    add_video(db, "1")
    add_video(db, "2")
    add_video(db, "3")
    write_run(db, "1")
    write_run(db, "1", "needs_human")
    assert states(db) == {"1": "pending", "2": "pending", "3": "held"}


def test_a_dispatch_that_gave_up_releases_the_next(db: psycopg.Connection) -> None:
    add_video(db, "1")
    add_video(db, "2")
    db.execute("UPDATE localization_outbox SET state = 'rejected' WHERE aweme_id = '1'")
    assert states(db) == {"1": "rejected", "2": "pending"}


def test_a_video_run_by_hand_does_not_advance_the_chain(db: psycopg.Connection) -> None:
    add_video(db, "1")
    add_video(db, "2")
    add_video(db, "3", "pending")  # localized by hand
    write_run(db, "3")
    assert states(db) == {"1": "pending", "2": "held", "3": "pending"}


def test_a_release_is_logged_like_a_dashboard_click(db: psycopg.Connection) -> None:
    add_video(db, "1")
    assert db.execute("SELECT action, detail FROM dashboard_actions").fetchall() == [
        ("localize", {"source": "auto_queue"})
    ]


def test_a_stuck_video_is_cleared_by_the_next_event_after_fourteen_days(
    db: psycopg.Connection,
) -> None:
    add_video(db, "1")
    add_video(db, "2")
    db.execute("UPDATE auto_queue_inflight SET released_at = now() - interval '15 days'")
    add_video(db, "3")
    assert states(db) == {"1": "pending", "2": "pending", "3": "held"}


def test_the_functions_are_not_callable_by_the_worker_role(db: psycopg.Connection) -> None:
    add_video(db, "1")
    db.execute("SET ROLE localization")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("SELECT auto_queue_release_next()")


def test_rollback_removes_everything_it_added(db: psycopg.Connection) -> None:
    db.execute(QUEUE_DOWN)
    triggers = db.execute("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'auto_queue%'")
    assert triggers.fetchone() == (0,)
    assert db.execute("SELECT to_regclass('auto_queue_inflight')").fetchone() == (None,)
    add_video(db, "1")
    assert states(db) == {"1": "held"}
