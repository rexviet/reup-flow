from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.scratch_db import scratch_database

CORE = (
    Path(__file__).resolve().parents[2] / "migrations" / "261026-localization-core.sql"
).read_text()


@pytest.fixture
def glossary_db() -> Iterator[tuple[str, "psycopg.Connection"]]:  # noqa: F821
    """A throwaway database with the real migration. The connection string makes
    every session run as the `localization` role, which is who the worker is."""
    for info, conn in scratch_database(CORE):
        yield f"{info} options='-c role=localization'", conn
