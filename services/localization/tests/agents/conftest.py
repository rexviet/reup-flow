from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.scratch_db import COLLECTIONS, INIT, scratch_database

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
CORE = (_MIGRATIONS / "261026-localization-core.sql").read_text()
GLOSSARY_SCOPE = (_MIGRATIONS / "261114-glossary-collection-scope.sql").read_text()
# Channels the test inputs name; each has its channel collection, as after the backfill.
CHANNELS = ("chan_1", "chan_2")


@pytest.fixture
def glossary_db() -> Iterator[tuple[str, "psycopg.Connection"]]:  # noqa: F821
    """A throwaway database with the real migrations, the glossary scoped by
    collection. The connection string makes every session run as the
    `localization` role, which is who the worker is."""
    for info, conn in scratch_database(INIT, COLLECTIONS, CORE, GLOSSARY_SCOPE):
        for channel in CHANNELS:
            conn.execute("INSERT INTO profiles (sec_user_id) VALUES (%s)", (channel,))
            conn.execute(
                "INSERT INTO collections (kind, external_id, sec_user_id, name) "
                "VALUES ('channel', %s, %s, %s)",
                (channel, channel, channel),
            )
        yield f"{info} options='-c role=localization'", conn
