"""Applies the migration to a THROWAWAY database, never to `reup`.

Needs the compose Postgres on localhost:5432 (the `localization` role already
exists there); skipped when it is not reachable.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg import sql
from psycopg.errors import CheckViolation

SERVICE = Path(__file__).resolve().parents[1]
UP = (SERVICE / "migrations" / "261026-localization-core.sql").read_text()
DOWN = (SERVICE / "migrations" / "261026-localization-core.down.sql").read_text()


def _admin_conninfo() -> str | None:
    env = {**dotenv_values(SERVICE.parents[1] / ".env"), **os.environ}
    user, password = env.get("POSTGRES_USER"), env.get("POSTGRES_PASSWORD")
    if not user or not password:
        return None
    host = env.get("LOCALIZATION_TEST_PG_HOST", "localhost")
    return f"host={host} port=5432 user={user} password={password} dbname=postgres"


def _unavailable(reason: str) -> None:
    """Locally a missing Postgres skips; in CI it is a failure, because a skipped
    migration test is a migration nobody tested."""
    if os.environ.get("CI"):
        pytest.fail(f"CI must run the migration test, but: {reason}")
    pytest.skip(reason)


@pytest.fixture
def scratch_db() -> Iterator[psycopg.Connection]:
    admin_info = _admin_conninfo()
    if admin_info is None:
        _unavailable("POSTGRES_USER / POSTGRES_PASSWORD are not available")
    try:
        admin = psycopg.connect(admin_info, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        _unavailable("Postgres is not reachable on localhost:5432")
    # The compose stack's db-init creates this role; a bare CI Postgres does not.
    admin.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'localization') "
        "THEN CREATE ROLE localization NOLOGIN; END IF; END $$"
    )
    name = f"loc_migration_{uuid.uuid4().hex[:12]}"
    assert name != "reup"
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    conn = psycopg.connect(admin_info.replace("dbname=postgres", f"dbname={name}"), autocommit=True)
    try:
        yield conn
    finally:
        conn.close()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _tables(conn: psycopg.Connection, schema: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = %s", (schema,)
    ).fetchall()
    return dict(rows)


def test_up_creates_owned_tables_and_down_removes_the_schema(
    scratch_db: psycopg.Connection,
) -> None:
    before_public = _tables(scratch_db, "public")
    scratch_db.execute(UP)
    assert _tables(scratch_db, "localization") == {
        "glossary": "localization",
        "voice_rates": "localization",
        "runs": "localization",
    }
    owner = scratch_db.execute(
        "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'localization'"
    ).fetchone()
    assert owner == ("localization",)

    scratch_db.execute(DOWN)
    assert (
        scratch_db.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'localization'").fetchone()
        is None
    )
    assert _tables(scratch_db, "public") == before_public


def test_glossary_limits_and_first_writer_wins(scratch_db: psycopg.Connection) -> None:
    scratch_db.execute(UP)
    insert = (
        "INSERT INTO localization.glossary "
        "(channel_id, term_zh, term_vi, note, source_aweme_id, workflow_id) "
        "VALUES ('c', %s, %s, %s, '1', 'video-1-g0') ON CONFLICT DO NOTHING"
    )
    scratch_db.execute(insert, ("你好", "xin chào", "greeting"))
    scratch_db.execute(insert, ("你好", "chào bạn", None))  # replay with another value
    assert scratch_db.execute("SELECT term_vi FROM localization.glossary").fetchall() == [
        ("xin chào",)
    ]
    with pytest.raises(CheckViolation):
        scratch_db.execute(insert, ("长", "x" * 65, None))
    with pytest.raises(CheckViolation):
        scratch_db.execute(insert, ("注", "ok", "n" * 201))
    scratch_db.execute(insert, ("边", "x" * 64, "n" * 200))  # the limits themselves are fine


def test_runs_are_keyed_by_video_and_generation(scratch_db: psycopg.Connection) -> None:
    scratch_db.execute(UP)
    insert = (
        "INSERT INTO localization.runs (aweme_id, generation, workflow_id, status) "
        "VALUES ('1', %s, 'w', 'running')"
    )
    scratch_db.execute(insert, (0,))
    scratch_db.execute(insert, (1,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        scratch_db.execute(insert, (1,))
    row = scratch_db.execute(
        "SELECT review_rounds, needs_human, verdict FROM localization.runs WHERE generation = 0"
    ).fetchone()
    assert row == (0, False, None)


def test_down_refuses_to_destroy_what_the_migration_did_not_create(
    scratch_db: psycopg.Connection,
) -> None:
    scratch_db.execute(UP)
    scratch_db.execute("CREATE TABLE localization.someone_elses (id int)")
    scratch_db.execute("CREATE TABLE public.bystander (id int)")
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        scratch_db.execute(DOWN)
    # The whole script is one implicit transaction: the failure also rolled the
    # table drops back, so nothing was lost.
    assert {"someone_elses", "runs", "glossary", "voice_rates"} <= set(
        _tables(scratch_db, "localization")
    )
    assert "bystander" in _tables(scratch_db, "public")


def test_down_leaves_other_schemas_alone(scratch_db: psycopg.Connection) -> None:
    scratch_db.execute("CREATE SCHEMA other")
    scratch_db.execute("CREATE TABLE other.keepme (id int)")
    scratch_db.execute(UP)
    scratch_db.execute(DOWN)
    assert list(_tables(scratch_db, "other")) == ["keepme"]
