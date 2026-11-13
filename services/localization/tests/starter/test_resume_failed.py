"""A failed run goes on from the step that failed: Temporal replays the steps that finished from
its history and schedules the failed one again, under the same workflow id."""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from localization.starter.outbox_starter import (
    CLEAR_FAILED_RUN_SQL,
    NOT_RESUMABLE_SQL,
    OutboxStarter,
    Start,
)
from tests.starter.resume_workflows import (
    QUEUE,
    ActivityRun,
    CodeRun,
    broken,
    calls,
    ingest,
    transcribe,
)
from tests.starter.test_sequential_queue import CORE
from tests.support.scratch_db import DASHBOARD_INTAKE, INIT, OUTBOX, scratch_database


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
    yield


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
