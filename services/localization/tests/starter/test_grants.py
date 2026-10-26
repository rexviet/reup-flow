"""The starter runs as the `localization` role, which owns nothing in the
application database. Its grants are exactly what the starter needs."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from localization.starter.outbox_starter import (
    CLAIM_SQL,
    DISPATCHED_SQL,
    FAILED_SQL,
    REJECTED_SQL,
    VIDEO_SQL,
)
from tests.support.scratch_db import INIT, OUTBOX, scratch_database

GRANTS = (
    Path(__file__).resolve().parents[2] / "migrations" / "261026-outbox-starter-grants.sql"
).read_text()


@pytest.fixture
def db() -> Iterator[psycopg.Connection]:
    for _, conn in scratch_database(INIT, OUTBOX, GRANTS):
        conn.execute("INSERT INTO profiles (sec_user_id) VALUES ('c')")
        conn.execute(
            "INSERT INTO videos (aweme_id, sec_user_id, create_time, storage_path, status) "
            "VALUES ('1', 'c', now(), 's3://b/source/1.mp4', 'done')"
        )
        conn.execute("INSERT INTO localization_outbox (aweme_id) VALUES ('1')")
        yield conn


def test_the_starter_can_run_every_statement_it_uses(db: psycopg.Connection) -> None:
    db.execute("SET ROLE localization")
    assert db.execute(CLAIM_SQL, {"minutes": 5}).fetchone() is not None
    assert db.execute(VIDEO_SQL, ("1",)).fetchone() == ("s3://b/source/1.mp4", "c")
    db.execute(DISPATCHED_SQL, ("video-1-g1", "1", 1))
    db.execute(REJECTED_SQL, ("x", "1", 1))
    db.execute(
        FAILED_SQL,
        {"error": "e", "max_attempts": 5, "delay": 30, "aweme_id": "1", "generation": 1},
    )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO localization_outbox (aweme_id) VALUES ('2')",
        "DELETE FROM localization_outbox",
        "UPDATE videos SET status = 'failed'",
        "SELECT * FROM jobs",
        "SELECT * FROM profiles",
    ],
)
def test_the_starter_can_do_nothing_else(db: psycopg.Connection, statement: str) -> None:
    db.execute("SET ROLE localization")
    with pytest.raises(InsufficientPrivilege):
        db.execute(statement)
