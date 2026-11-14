"""A throwaway Postgres database holding the downloader's real schema and the
outbox migration, so the starter is tested against the tables it runs against."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg import sql

REPO = Path(__file__).resolve().parents[4]
MIGRATIONS = REPO / "services" / "downloader" / "migrations"
INIT = (MIGRATIONS / "260824-init-schema.sql").read_text()
OUTBOX = (MIGRATIONS / "261026-localization-outbox.sql").read_text()
DASHBOARD_INTAKE = (MIGRATIONS / "261112-dashboard-intake.sql").read_text()
# The collections table as the code reads it: the table and the columns added to it since.
COLLECTIONS = "\n".join(
    (MIGRATIONS / name).read_text()
    for name in ("261114-collections.sql", "261114-collection-cover-geometry.sql")
)


def add_channel_collection(
    conn: psycopg.Connection, channel: str, name: str | None = None, **settings: object
) -> str:
    """The channel's profile and channel collection, as after the collections backfill,
    with these collection columns set. Returns the collection id; a second call for the
    same channel returns the first one's collection and changes nothing."""
    conn.execute(
        "INSERT INTO profiles (sec_user_id) VALUES (%s) ON CONFLICT DO NOTHING", (channel,)
    )
    columns = {"kind": "channel", "external_id": channel, "sec_user_id": channel}
    columns |= {"name": name or channel, **settings}
    conn.execute(
        sql.SQL(
            "INSERT INTO collections ({}) VALUES ({}) ON CONFLICT (kind, external_id) DO NOTHING"
        ).format(
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.SQL(", ").join(sql.Placeholder() * len(columns)),
        ),
        list(columns.values()),
    )
    row = conn.execute(
        "SELECT id::text FROM collections WHERE kind = 'channel' AND external_id = %s", (channel,)
    ).fetchone()
    assert row is not None
    return row[0]


def admin_conninfo() -> str | None:
    env = dict(os.environ)
    if not env.get("POSTGRES_USER") or not env.get("POSTGRES_PASSWORD"):
        env = {**dotenv_values(REPO / ".env"), **env}
    user, password = env.get("POSTGRES_USER"), env.get("POSTGRES_PASSWORD")
    if not user or not password:
        return None
    host = env.get("LOCALIZATION_TEST_PG_HOST", "localhost")
    port = env.get("LOCALIZATION_TEST_PG_PORT", "5432")
    return f"host={host} port={port} user={user} password={password} dbname=postgres"


def unavailable(reason: str) -> None:
    """Locally a missing Postgres skips; in CI it is a failure, because a skipped
    starter test is a starter nobody tested."""
    if os.environ.get("CI"):
        pytest.fail(f"CI must run this test, but: {reason}")
    pytest.skip(reason)


def scratch_database(*scripts: str) -> Iterator[tuple[str, psycopg.Connection]]:
    """Yields (conninfo, admin connection to the new database)."""
    admin_info = admin_conninfo()
    if admin_info is None:
        unavailable("POSTGRES_USER / POSTGRES_PASSWORD are not available")
    try:
        admin = psycopg.connect(admin_info, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        unavailable("Postgres is not reachable on configured test host/port")
    name = f"loc_starter_{uuid.uuid4().hex[:12]}"
    assert name != "reup"
    admin.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'localization') "
        "THEN CREATE ROLE localization NOLOGIN; END IF; END $$"
    )
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    info = admin_info.replace("dbname=postgres", f"dbname={name}")
    conn = psycopg.connect(info, autocommit=True)
    try:
        for script in scripts:
            conn.execute(script)
        yield info, conn
    finally:
        conn.close()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
