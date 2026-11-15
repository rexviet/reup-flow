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

from tests.support.scratch_db import COLLECTIONS, INIT
from tests.support.scratch_db import MIGRATIONS as REPO_MIGRATIONS

SERVICE = Path(__file__).resolve().parents[1]
UP = (SERVICE / "migrations" / "261026-localization-core.sql").read_text()
DOWN = (SERVICE / "migrations" / "261026-localization-core.down.sql").read_text()


def _admin_conninfo() -> str | None:
    env = dict(os.environ)
    if not env.get("POSTGRES_USER") or not env.get("POSTGRES_PASSWORD"):
        env = {**dotenv_values(SERVICE.parents[1] / ".env"), **env}
    user, password = env.get("POSTGRES_USER"), env.get("POSTGRES_PASSWORD")
    if not user or not password:
        return None
    host = env.get("LOCALIZATION_TEST_PG_HOST", "localhost")
    port = env.get("LOCALIZATION_TEST_PG_PORT", "5432")
    return f"host={host} port={port} user={user} password={password} dbname=postgres"


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
        _unavailable("Postgres is not reachable on configured test host/port")
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


def test_review_project_column_is_additive_and_down_preserves_legacy(scratch_db):
    scratch_db.execute(UP)
    scratch_db.execute(
        "INSERT INTO localization.runs(aweme_id,generation,workflow_id,status,review_key) "
        "VALUES ('7001',1,'legacy','needs_human','review/7001-g1.mp4')"
    )
    scratch_db.execute((SERVICE / "migrations/261108-review-project-key.sql").read_text())
    assert scratch_db.execute(
        "SELECT project_key,review_key FROM localization.runs"
    ).fetchone() == (None, "review/7001-g1.mp4")
    scratch_db.execute((SERVICE / "migrations/261108-review-project-key.down.sql").read_text())
    assert scratch_db.execute("SELECT review_key FROM localization.runs").fetchone() == (
        "review/7001-g1.mp4",
    )


# --- glossary scoped by collection -------------------------------------------

SCOPE = (SERVICE / "migrations" / "261114-glossary-collection-scope.sql").read_text()
SCOPE_DOWN = (SERVICE / "migrations" / "261114-glossary-collection-scope.down.sql").read_text()
_GLOSSARY_INSERT = (
    "INSERT INTO localization.glossary "
    "(channel_id, term_zh, term_vi, source_aweme_id, workflow_id, created_at) "
    "VALUES (%s, %s, %s, '1', 'video-1-g1', %s)"
)


def _legacy_glossary(conn: psycopg.Connection) -> None:
    """Two channels with the glossary keyed by channel, as before collections."""
    conn.execute(INIT)
    conn.execute(UP)
    conn.execute("INSERT INTO profiles (sec_user_id) VALUES ('sec-a'), ('sec-b')")
    for channel, zh, vi in (
        ("sec-a", "苹果", "Apple"),
        ("sec-a", "华为", "Huawei"),
        ("sec-b", "苹果", "quả táo"),
    ):
        conn.execute(_GLOSSARY_INSERT, (channel, zh, vi, "2026-11-01"))


def _columns(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'localization' AND table_name = 'glossary'"
    ).fetchall()
    return dict(rows)


def _primary_key(conn: psycopg.Connection) -> str:
    return conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'localization.glossary'::regclass AND contype = 'p'"
    ).fetchone()[0]


def test_glossary_moves_to_the_channel_collection_without_losing_a_row(
    scratch_db: psycopg.Connection,
) -> None:
    _legacy_glossary(scratch_db)
    scratch_db.execute(COLLECTIONS)
    scratch_db.execute(SCOPE)

    assert "channel_id" not in _columns(scratch_db)
    assert _columns(scratch_db)["collection_id"] == "uuid"
    assert _primary_key(scratch_db) == "PRIMARY KEY (collection_id, term_zh)"
    rows = scratch_db.execute(
        "SELECT c.kind, c.external_id, g.term_zh, g.term_vi FROM localization.glossary g "
        "JOIN public.collections c ON c.id = g.collection_id ORDER BY 2, 3"
    ).fetchall()
    assert rows == [
        ("channel", "sec-a", "华为", "Huawei"),
        ("channel", "sec-a", "苹果", "Apple"),
        ("channel", "sec-b", "苹果", "quả táo"),
    ]


def test_glossary_refuses_a_term_for_a_collection_that_does_not_exist(
    scratch_db: psycopg.Connection,
) -> None:
    _legacy_glossary(scratch_db)
    scratch_db.execute(COLLECTIONS)
    scratch_db.execute(SCOPE)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        scratch_db.execute(
            "INSERT INTO localization.glossary "
            "(collection_id, term_zh, term_vi, source_aweme_id, workflow_id) "
            "VALUES (gen_random_uuid(), '孤', 'mồ côi', '1', 'w')"
        )


def test_glossary_rows_are_writable_by_the_worker_role_after_the_move(
    scratch_db: psycopg.Connection,
) -> None:
    """The worker writes as `localization`: the foreign key to a table it does not
    own must not turn its inserts into permission errors."""
    _legacy_glossary(scratch_db)
    scratch_db.execute(COLLECTIONS)
    scratch_db.execute(SCOPE)
    scratch_db.execute("SET ROLE localization")
    scratch_db.execute(
        "INSERT INTO localization.glossary "
        "(collection_id, term_zh, term_vi, source_aweme_id, workflow_id) "
        "SELECT id, '小米', 'Xiaomi', 'dashboard', 'dashboard' FROM public.collections "
        "WHERE kind = 'channel' AND external_id = 'sec-b'"
    )
    scratch_db.execute("RESET ROLE")
    assert scratch_db.execute("SELECT count(*) FROM localization.glossary").fetchone() == (4,)


def test_glossary_move_stops_with_a_count_when_a_channel_has_no_collection(
    scratch_db: psycopg.Connection,
) -> None:
    _legacy_glossary(scratch_db)
    for zh in ("一", "二"):
        scratch_db.execute(_GLOSSARY_INSERT, ("ghost", zh, "x", "2026-11-01"))
    scratch_db.execute(COLLECTIONS)
    with pytest.raises(psycopg.errors.RaiseException, match="2 glossary rows"):
        scratch_db.execute(SCOPE)
    scratch_db.execute("ROLLBACK")  # the script's own transaction is left aborted
    assert "channel_id" in _columns(scratch_db)
    assert scratch_db.execute("SELECT count(*) FROM localization.glossary").fetchone() == (5,)


def test_glossary_down_keeps_the_channel_row_then_the_oldest_and_reports_what_it_drops(
    scratch_db: psycopg.Connection,
) -> None:
    _legacy_glossary(scratch_db)
    scratch_db.execute(COLLECTIONS)
    scratch_db.execute(SCOPE)
    scratch_db.execute(
        "INSERT INTO public.collections (kind, external_id, sec_user_id, name) VALUES "
        "('mix', 'mix-1', 'sec-a', 'Mix 1'), ('mix', 'mix-2', 'sec-a', 'Mix 2')"
    )
    scratch_db.execute("INSERT INTO public.collections (kind, name) VALUES ('manual', 'Tay')")

    def term(collection_sql: str, zh: str, vi: str, created: str) -> None:
        scratch_db.execute(
            "INSERT INTO localization.glossary "
            "(collection_id, term_zh, term_vi, source_aweme_id, workflow_id, created_at) "
            "SELECT id, %s, %s, 'dashboard', 'dashboard', %s FROM public.collections "
            f"WHERE {collection_sql}",
            (zh, vi, created),
        )

    # Older than the channel's own row, but the channel's row still wins.
    term("external_id = 'mix-1'", "苹果", "Táo mix", "2026-10-01")
    # Only in mixes of sec-a: the oldest one survives as sec-a's term.
    term("external_id = 'mix-2'", "成名", "Thành danh mới", "2026-11-05")
    term("external_id = 'mix-1'", "成名", "Thành danh", "2026-11-02")
    # A manual collection without a channel has nowhere to go.
    term("kind = 'manual'", "孤", "mồ côi", "2026-11-03")

    notices: list[str] = []
    scratch_db.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
    scratch_db.execute(SCOPE_DOWN)

    assert _columns(scratch_db)["channel_id"] == "text"
    assert "collection_id" not in _columns(scratch_db)
    assert _primary_key(scratch_db) == "PRIMARY KEY (channel_id, term_zh)"
    assert scratch_db.execute(
        "SELECT channel_id, term_zh, term_vi FROM localization.glossary ORDER BY 1, 2"
    ).fetchall() == [
        ("sec-a", "华为", "Huawei"),
        ("sec-a", "成名", "Thành danh"),
        ("sec-a", "苹果", "Apple"),
        ("sec-b", "苹果", "quả táo"),
    ]
    assert any("dropped 3 rows" in n for n in notices), notices
    # Nothing references collections any more, so the downloader side can roll back too.
    scratch_db.execute((REPO_MIGRATIONS / "261114-collections.down.sql").read_text())
