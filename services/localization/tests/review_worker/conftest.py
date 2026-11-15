import threading
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from localization.review_bridge.repository import Repository
from localization.review_worker.config import WorkerConfig
from tests.support.review_project import project_inputs
from tests.support.scratch_db import DASHBOARD_INTAKE, INIT, OUTBOX, scratch_database

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
CORE = (MIGRATIONS / "261026-localization-core.sql").read_text()
PROJECT_KEY = (MIGRATIONS / "261108-review-project-key.sql").read_text()
DELIVERY = (MIGRATIONS / "261112-review-delivery.sql").read_text()


class Db:
    """`admin` is the superuser that plays the downloader; `worker` connects as the
    `localization` role the real worker uses."""

    def __init__(self, info, admin):
        self.info, self.admin = info, admin
        self.opened: list[psycopg.Connection] = []

    def worker(self) -> psycopg.Connection:
        conn = psycopg.connect(
            f"{self.info} options='-c role=localization'", autocommit=True, row_factory=dict_row
        )
        self.opened.append(conn)
        return conn

    def approve(self, aweme="7001", generation=1, revision="a" * 32, *, queued=True):
        self.admin.execute(
            "INSERT INTO review_decisions (aweme_id, generation, decision, approved_revision) "
            "VALUES (%s, %s, 'approved', %s) ON CONFLICT (aweme_id, generation) "
            "DO UPDATE SET decision = 'approved', approved_revision = EXCLUDED.approved_revision",
            (aweme, generation, revision),
        )
        if queued:
            self.queue(aweme, generation, revision)

    def queue(self, aweme="7001", generation=1, revision="a" * 32):
        self.admin.execute(
            "INSERT INTO localization.final_renders (aweme_id, generation, approved_revision) "
            "VALUES (%s, %s, %s) ON CONFLICT (aweme_id, generation) DO UPDATE "
            "SET approved_revision = EXCLUDED.approved_revision, state = 'queued', attempts = 0",
            (aweme, generation, revision),
        )

    def render_row(self, aweme="7001", generation=1):
        return self.admin.execute(
            "SELECT * FROM localization.final_renders WHERE aweme_id = %s AND generation = %s",
            (aweme, generation),
        ).fetchone()


@pytest.fixture
def db() -> Iterator[Db]:
    for info, admin in scratch_database(
        INIT, OUTBOX, DASHBOARD_INTAKE, CORE, PROJECT_KEY, DELIVERY
    ):
        admin.row_factory = dict_row
        state = Db(info, admin)
        yield state
        for conn in state.opened:
            conn.close()


@pytest.fixture
def cfg(tmp_path) -> WorkerConfig:
    data = tmp_path / "data" / "review"
    data.mkdir(parents=True)
    return WorkerConfig(
        database_url="unused",
        data_root=data,
        work_root=tmp_path / "data" / "review-render",
        hyperframes_bin="/bin/hyperframes.mjs",
        renew_every_s=0,
    )


@pytest.fixture
def cancelled() -> threading.Event:
    return threading.Event()


@pytest.fixture
def project(review_store, tmp_path):
    """A real review project in a mock bucket; returns (repo, revision)."""
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    builder._build(spec, dub, ingest, subs, "engine")
    repo = Repository(review_store, tmp_path / "data" / "review")
    return repo, repo.current("7001", 1)[1].revision_id
