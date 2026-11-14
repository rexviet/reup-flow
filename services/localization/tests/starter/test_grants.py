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
    RELEASED_SQL,
    VIDEO_SQL,
)
from tests.support.scratch_db import COLLECTIONS, INIT, OUTBOX, scratch_database

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
    db.execute(RELEASED_SQL, {"error": "e", "delay": 30, "aweme_id": "1", "generation": 1})
    db.execute(
        FAILED_SQL,
        {"error": "e", "max_attempts": 5, "delay": 30, "aweme_id": "1", "generation": 1},
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE localization_outbox SET generation = 9",  # re-running a video is an operator's
        'SELECT "desc", play_addr FROM videos',
        "SELECT enqueued_at FROM localization_outbox",
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


# --- collections and folder exports ------------------------------------------
# The Mac worker runs as `localization` too: it reads a video's collection, the
# collection's settings, and claims and settles folder exports. What to export
# and where is decided by the downloader and must stay out of its reach.


@pytest.fixture
def collections_db() -> Iterator[psycopg.Connection]:
    for _, conn in scratch_database(INIT, OUTBOX, GRANTS, COLLECTIONS):
        conn.execute("INSERT INTO profiles (sec_user_id) VALUES ('c')")
        conn.execute(
            "INSERT INTO collections (kind, external_id, sec_user_id, name, save_subdir) "
            "VALUES ('channel', 'c', 'c', 'Kênh', 'Drama')"
        )
        conn.execute(
            "INSERT INTO videos (aweme_id, sec_user_id, create_time, storage_path, status, "
            "collection_id, mix_episode) "
            "SELECT '1', 'c', now(), 's3://b/source/1.mp4', 'done', id, 3 FROM collections"
        )
        conn.execute(
            "INSERT INTO folder_exports (aweme_id, generation, collection_id, target_subdir, "
            "file_stem, final_key) SELECT '1', 1, id, 'Drama', 'E03-1', "
            "'output/review/1/g1/final.mp4' FROM collections"
        )
        yield conn


def test_the_worker_reads_a_videos_collection_and_its_settings(
    collections_db: psycopg.Connection,
) -> None:
    collections_db.execute("SET ROLE localization")
    row = collections_db.execute(
        "SELECT v.mix_episode, c.name, c.save_subdir, c.bgm_offset_db, c.caption_text_color, "
        "c.cover_color, c.cover_opacity_pct FROM videos v JOIN collections c "
        "ON c.id = v.collection_id WHERE v.aweme_id = '1'"
    ).fetchone()
    assert row == (3, "Kênh", "Drama", None, None, None, None)


def test_the_worker_claims_and_settles_a_folder_export(
    collections_db: psycopg.Connection,
) -> None:
    collections_db.execute("SET ROLE localization")
    claimed = collections_db.execute(
        "UPDATE folder_exports SET state = 'exporting', claim = gen_random_uuid(), "
        "lease_expires_at = now() + interval '5 minutes' "
        "WHERE id = (SELECT id FROM folder_exports WHERE state = 'pending' "
        "OR (state = 'exporting' AND lease_expires_at < now()) ORDER BY id LIMIT 1 "
        "FOR UPDATE SKIP LOCKED) "
        "RETURNING id, claim, target_subdir, file_stem, final_key, srt_key"
    ).fetchone()
    assert claimed is not None
    assert claimed[2:] == ("Drama", "E03-1", "output/review/1/g1/final.mp4", None)
    settled = collections_db.execute(
        "UPDATE folder_exports SET state = 'done', error = NULL, handled_at = now(), "
        "files = '[\"Drama/E03-1.mp4\"]'::jsonb WHERE id = %s AND claim = %s RETURNING state",
        (claimed[0], claimed[1]),
    ).fetchone()
    assert settled == ("done",)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE folder_exports SET target_subdir = '../../etc'",
        "UPDATE folder_exports SET file_stem = 'E01-2'",
        "UPDATE folder_exports SET final_key = 'secret/other.mp4'",
        "UPDATE folder_exports SET aweme_id = '2'",
        "INSERT INTO folder_exports (aweme_id, generation, collection_id, target_subdir, "
        "file_stem, final_key) SELECT '1', 2, id, 'x', '1', 'k' FROM collections",
        "DELETE FROM folder_exports",
        "UPDATE collections SET save_subdir = 'Other'",
        "INSERT INTO collections (kind, name) VALUES ('manual', 'x')",
        "DELETE FROM collections",
        "UPDATE videos SET collection_id = NULL",
        "UPDATE videos SET mix_episode = 1",
    ],
)
def test_the_worker_cannot_change_what_is_exported_or_where(
    collections_db: psycopg.Connection, statement: str
) -> None:
    collections_db.execute("SET ROLE localization")
    with pytest.raises(InsufficientPrivilege):
        collections_db.execute(statement)


def test_the_worker_cannot_pin_collections_with_its_own_foreign_keys(
    collections_db: psycopg.Connection,
) -> None:
    """The glossary's foreign key is created by an admin; the worker role needs only
    SELECT. With REFERENCES it could add keys of its own that block deleting or
    re-keying a collection."""
    collections_db.execute("CREATE SCHEMA worker_owned AUTHORIZATION localization")
    collections_db.execute("SET ROLE localization")
    with pytest.raises(InsufficientPrivilege):
        collections_db.execute(
            "CREATE TABLE worker_owned.pins (collection_id uuid REFERENCES public.collections (id))"
        )
