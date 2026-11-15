"""The switch on the sequential queue: off, a finished run releases nothing; on again, the
next held video starts at once."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from tests.starter.test_sequential_queue import add_video, states, write_run
from tests.support.scratch_db import DASHBOARD_INTAKE, INIT, OUTBOX, scratch_database

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
CORE = (MIGRATIONS / "261026-localization-core.sql").read_text()
QUEUE = (MIGRATIONS / "261113-sequential-queue.sql").read_text()
SWITCH = (MIGRATIONS / "261114-auto-queue-switch.sql").read_text()
SWITCH_DOWN = (MIGRATIONS / "261114-auto-queue-switch.down.sql").read_text()


@pytest.fixture
def db() -> Iterator[psycopg.Connection]:
    for _, conn in scratch_database(INIT, OUTBOX, DASHBOARD_INTAKE, CORE, QUEUE, SWITCH):
        conn.execute("INSERT INTO profiles (sec_user_id) VALUES ('c')")
        yield conn


def switch(db: psycopg.Connection, on: bool) -> None:
    """What the dashboard does: set the switch and, when turning it on, release."""
    db.execute("UPDATE auto_queue_settings SET enabled = %s, updated_at = now()", (on,))
    if on:
        db.execute("SELECT auto_queue_release_next()")


def test_the_switch_is_on_by_default_and_the_queue_runs_as_before(db: psycopg.Connection) -> None:
    assert db.execute("SELECT enabled FROM auto_queue_settings").fetchall() == [(True,)]
    add_video(db, "1")
    add_video(db, "2")
    write_run(db, "1")
    assert states(db) == {"1": "pending", "2": "pending"}


def test_off_a_finished_run_releases_nothing_and_new_held_videos_wait(
    db: psycopg.Connection,
) -> None:
    add_video(db, "1")
    add_video(db, "2")
    switch(db, False)
    write_run(db, "1")
    add_video(db, "3")
    assert states(db) == {"1": "pending", "2": "held", "3": "held"}
    # The finished video is no longer in flight, so nothing blocks a later release.
    assert db.execute("SELECT count(*) FROM auto_queue_inflight").fetchone() == (0,)


def test_on_again_the_next_held_video_starts_at_once(db: psycopg.Connection) -> None:
    add_video(db, "1")
    add_video(db, "2")
    switch(db, False)
    write_run(db, "1")
    switch(db, True)
    assert states(db) == {"1": "pending", "2": "pending"}


def test_on_again_while_a_video_is_still_in_flight_waits_for_it(db: psycopg.Connection) -> None:
    add_video(db, "1")
    add_video(db, "2")
    switch(db, False)
    switch(db, True)
    assert states(db) == {"1": "pending", "2": "held"}
    write_run(db, "1")
    assert states(db)["2"] == "pending"


def test_off_with_nothing_in_flight_a_new_held_video_does_not_start(
    db: psycopg.Connection,
) -> None:
    switch(db, False)
    add_video(db, "1")
    assert states(db) == {"1": "held"}


def test_the_worker_role_cannot_flip_the_switch(db: psycopg.Connection) -> None:
    db.execute("SET ROLE localization")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("UPDATE auto_queue_settings SET enabled = false")


def test_rollback_drops_the_switch_and_the_queue_runs_unconditionally(
    db: psycopg.Connection,
) -> None:
    switch(db, False)
    db.execute(SWITCH_DOWN)
    assert db.execute("SELECT to_regclass('auto_queue_settings')").fetchone() == (None,)
    add_video(db, "1")
    assert states(db) == {"1": "pending"}
