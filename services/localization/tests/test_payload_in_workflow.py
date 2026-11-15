"""An oversize payload produced INSIDE a workflow.

`SizeGuardCodec` raises while the workflow's commands are encoded, which Temporal
treats as a failed workflow task and retries forever: the workflow never fails,
it just stays RUNNING. The first two tests pin that real behaviour on a real
worker. The rest show the pre-check turns it into a clean, non-retryable failure.
"""

import asyncio
import uuid

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from tests.support.big_flow import TASK_QUEUE, BigFlow


@activity.defn(name="noop")
async def noop(value: str) -> None:
    return None


async def _start(client: Client, mode: str) -> WorkflowHandle:
    return await client.start_workflow(
        BigFlow.run,
        mode,
        id=f"big-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )


async def _task_failures(handle: WorkflowHandle) -> int:
    history = await handle.fetch_history()
    return sum(
        1 for e in history.events if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED
    )


async def _pending_task_attempt(handle: WorkflowHandle) -> int:
    """How many times the server has handed the same workflow task out. A failed
    task is retried without adding history, so this is where the loop shows."""
    description = await handle.describe()
    return description.raw_description.pending_workflow_task.attempt


async def _wait_for_attempts(handle: WorkflowHandle, at_least: int) -> int:
    attempt = 0
    for _ in range(300):
        attempt = await _pending_task_attempt(handle)
        if attempt >= at_least:
            break
        await asyncio.sleep(0.1)
    return attempt


@pytest.mark.parametrize("mode", ["unguarded_arg", "unguarded_result"])
async def test_unguarded_oversize_payload_leaves_the_workflow_retrying_forever(
    temporal_client: Client, mode: str
) -> None:
    async with Worker(
        temporal_client, task_queue=TASK_QUEUE, workflows=[BigFlow], activities=[noop]
    ):
        handle = await _start(temporal_client, mode)
        try:
            attempts = await _wait_for_attempts(handle, at_least=2)
            assert attempts >= 2, "the same workflow task should keep being retried"
            assert await _task_failures(handle) >= 1
            assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
        finally:
            await handle.terminate("test cleanup")


@pytest.mark.parametrize("mode", ["guarded_arg", "guarded_result"])
async def test_precheck_fails_the_workflow_fast_and_non_retryably(
    temporal_client: Client, mode: str
) -> None:
    async with Worker(
        temporal_client, task_queue=TASK_QUEUE, workflows=[BigFlow], activities=[noop]
    ):
        handle = await _start(temporal_client, mode)
        with pytest.raises(WorkflowFailureError) as info:
            await asyncio.wait_for(handle.result(), timeout=20)
        cause = info.value.cause
        assert isinstance(cause, ApplicationError)
        assert cause.type == "PayloadTooLarge"
        assert cause.non_retryable is True
        assert await _task_failures(handle) == 0
        assert (await handle.describe()).status == WorkflowExecutionStatus.FAILED
