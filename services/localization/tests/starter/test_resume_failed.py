"""A failed run goes on from the step that failed: Temporal replays the steps that finished from
its history and schedules the failed one again, under the same workflow id."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from localization.activities.skip_bypass import BYPASS_SQL
from localization.starter.outbox_starter import (
    BYPASSABLE_SKIPS,
    CLAIM_SQL,
    CLEAR_FAILED_RUN_SQL,
    CLEAR_SKIPPED_RUN_SQL,
    NOT_RESUMABLE_SQL,
    OutboxStarter,
    Start,
)
from tests.starter.resume_workflows import (
    QUEUE,
    ActivityRun,
    CodeRun,
    GatedRun,
    broken,
    calls,
    classify_video,
    ingest,
    skip_check,
    transcribe,
)
from tests.starter.test_sequential_queue import CORE
from tests.support.scratch_db import (
    BYPASS_SKIPS_GRANT,
    DASHBOARD_INTAKE,
    INIT,
    OUTBOX,
    scratch_database,
)

OUTBOX_GRANTS = (
    Path(__file__).resolve().parents[2] / "migrations" / "261026-outbox-starter-grants.sql"
).read_text()


def starter(client: Client) -> OutboxStarter:
    return OutboxStarter("postgresql://unused", client)


async def run_to_failure(client: Client, kind: type, id_: str) -> None:
    handle = await client.start_workflow(kind.run, id=id_, task_queue=QUEUE)
    with pytest.raises(Exception):  # noqa: B017 - WorkflowFailureError, whatever its cause
        await handle.result()


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    calls.clear()
    broken["transcribe"] = True
    skip_check.update(skip=True, bypassed=False)
    yield


def gated_worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=QUEUE,
        workflows=[ActivityRun, CodeRun, GatedRun],
        activities=[ingest, classify_video, transcribe],
    )


async def test_a_failed_run_replays_what_finished_and_runs_the_failed_step_again(
    temporal_client: Client,
) -> None:
    id_ = f"video-1-g1-{uuid.uuid4()}"
    async with Worker(
        temporal_client,
        task_queue=QUEUE,
        workflows=[ActivityRun, CodeRun],
        activities=[ingest, transcribe],
    ):
        await run_to_failure(temporal_client, ActivityRun, id_)
        assert calls == {"ingest": 1, "transcribe": 1}

        broken["transcribe"] = False  # the provider is fixed
        assert await starter(temporal_client).resume_if_failed(id_) is Start.RESUMED
        result = await temporal_client.get_workflow_handle(id_).result()

    assert result == "transcript of ingested"
    # The finished step was not run again; only the one that failed was.
    assert calls == {"ingest": 1, "transcribe": 2}


async def test_a_run_that_did_not_fail_in_a_step_cannot_be_gone_on_from(
    temporal_client: Client,
) -> None:
    id_ = f"video-2-g1-{uuid.uuid4()}"
    async with Worker(
        temporal_client, task_queue=QUEUE, workflows=[ActivityRun, CodeRun], activities=[]
    ):
        await run_to_failure(temporal_client, CodeRun, id_)
        assert await starter(temporal_client).resume_if_failed(id_) is Start.NOT_RESUMABLE


async def test_a_run_that_is_not_failed_is_a_duplicate_dispatch(temporal_client: Client) -> None:
    id_ = f"video-3-g1-{uuid.uuid4()}"
    broken["transcribe"] = False
    async with Worker(
        temporal_client,
        task_queue=QUEUE,
        workflows=[ActivityRun, CodeRun],
        activities=[ingest, transcribe],
    ):
        handle = await temporal_client.start_workflow(ActivityRun.run, id=id_, task_queue=QUEUE)
        await handle.result()
        assert await starter(temporal_client).resume_if_failed(id_) is Start.DUPLICATE
    assert calls == {"ingest": 1, "transcribe": 1}


async def test_a_skipped_run_goes_on_from_its_skip_check_when_skips_are_set_aside(
    temporal_client: Client,
) -> None:
    id_ = f"video-4-g1-{uuid.uuid4()}"
    broken["transcribe"] = False
    async with gated_worker(temporal_client):
        handle = await temporal_client.start_workflow(GatedRun.run, id=id_, task_queue=QUEUE)
        assert await handle.result() == "skipped"

        skip_check["bypassed"] = True  # the operator set the skips aside
        assert (
            await starter(temporal_client).resume_if_failed(id_, bypass_skips=True)
            is Start.UNSKIPPED
        )
        result = await temporal_client.get_workflow_handle(id_).result()

    assert result == "transcript of ingested"
    # What was prepared was kept; only the check was asked again, and the run went on.
    assert calls == {"ingest": 1, "classify_video": 2, "transcribe": 1}


async def test_a_finished_run_is_not_reset_unless_its_skips_are_set_aside(
    temporal_client: Client,
) -> None:
    id_ = f"video-5-g1-{uuid.uuid4()}"
    async with gated_worker(temporal_client):
        handle = await temporal_client.start_workflow(GatedRun.run, id=id_, task_queue=QUEUE)
        assert await handle.result() == "skipped"
        assert await starter(temporal_client).resume_if_failed(id_) is Start.DUPLICATE
    assert calls == {"ingest": 1, "classify_video": 1}


async def test_a_finished_run_with_no_skip_check_cannot_have_its_skips_set_aside(
    temporal_client: Client,
) -> None:
    id_ = f"video-6-g1-{uuid.uuid4()}"
    broken["transcribe"] = False
    async with gated_worker(temporal_client):
        handle = await temporal_client.start_workflow(ActivityRun.run, id=id_, task_queue=QUEUE)
        await handle.result()
        assert (
            await starter(temporal_client).resume_if_failed(id_, bypass_skips=True)
            is Start.NOT_RESUMABLE
        )
    assert calls == {"ingest": 1, "transcribe": 1}


# --- what the starter's database role may do for it ----------------------------------


@pytest.fixture
def db() -> Iterator[psycopg.Connection]:
    for _, conn in scratch_database(INIT, OUTBOX, DASHBOARD_INTAKE, CORE):
        conn.execute("INSERT INTO profiles (sec_user_id) VALUES ('c')")
        conn.execute(
            "INSERT INTO videos (aweme_id, sec_user_id, create_time, storage_path, status) "
            "VALUES ('1', 'c', now(), 's3://b/source/1.mp4', 'done')"
        )
        conn.execute("INSERT INTO localization_outbox (aweme_id, state) VALUES ('1', 'dispatched')")
        yield conn


def test_a_resume_removes_only_the_failed_run_row(db: psycopg.Connection) -> None:
    grants = (
        "GRANT SELECT (aweme_id, generation, state) ON localization_outbox TO localization; "
        "GRANT UPDATE (last_error) ON localization_outbox TO localization"
    )
    db.execute(grants)
    for generation, status in ((1, "failed"), (2, "ready_for_review")):
        db.execute(
            "INSERT INTO localization.runs (aweme_id, generation, workflow_id, status) "
            "VALUES ('1', %s, 'w', %s)",
            (generation, status),
        )
    db.execute("SET ROLE localization")
    db.execute(CLEAR_FAILED_RUN_SQL, ("1", 1))
    db.execute(CLEAR_FAILED_RUN_SQL, ("1", 2))  # a run that is not failed stays
    db.execute(NOT_RESUMABLE_SQL, ("why", "1", 1))
    db.execute("RESET ROLE")
    assert db.execute("SELECT generation, status FROM localization.runs").fetchall() == [
        (2, "ready_for_review")
    ]
    assert db.execute("SELECT last_error FROM localization_outbox").fetchone() == ("why",)


@pytest.fixture
def bypass_db() -> Iterator[psycopg.Connection]:
    for _, conn in scratch_database(INIT, OUTBOX, DASHBOARD_INTAKE, CORE):
        conn.execute(OUTBOX_GRANTS)
        conn.execute(BYPASS_SKIPS_GRANT)
        conn.execute("INSERT INTO profiles (sec_user_id) VALUES ('c')")
        conn.execute(
            "INSERT INTO videos (aweme_id, sec_user_id, create_time, storage_path, status) "
            "VALUES ('1', 'c', now(), 's3://b/source/1.mp4', 'done')"
        )
        yield conn


def test_the_starter_and_the_check_read_the_flag_and_clear_only_the_skipped_row(
    bypass_db: psycopg.Connection,
) -> None:
    db = bypass_db
    db.execute(
        "INSERT INTO localization_outbox (aweme_id, generation, state, bypass_skips) "
        "VALUES ('1', 2, 'pending', true)"
    )
    for generation, status in ((1, "skipped_ad"), (2, "skipped_ad"), (3, "skipped_no_video")):
        db.execute(
            "INSERT INTO localization.runs (aweme_id, generation, workflow_id, status) "
            "VALUES ('1', %s, 'w', %s)",
            (generation, status),
        )
    db.execute("SET ROLE localization")
    claimed = db.execute(CLAIM_SQL, {"minutes": 5}).fetchone()
    flag = db.execute(BYPASS_SQL, ("1", 2)).fetchone()
    other_generation = db.execute(BYPASS_SQL, ("1", 1)).fetchone()
    db.execute(CLEAR_SKIPPED_RUN_SQL, ("1", 2, list(BYPASSABLE_SKIPS)))
    db.execute(CLEAR_SKIPPED_RUN_SQL, ("1", 3, list(BYPASSABLE_SKIPS)))  # not one to set aside
    db.execute("RESET ROLE")

    assert claimed[-1] is True
    assert flag == (True,)
    assert other_generation is None
    assert db.execute(
        "SELECT generation, status FROM localization.runs ORDER BY generation"
    ).fetchall() == [(1, "skipped_ad"), (3, "skipped_no_video")]
