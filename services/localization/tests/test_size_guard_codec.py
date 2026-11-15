import uuid

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker

from localization.temporal.size_guard_codec import MAX_PAYLOAD_BYTES

QUEUE = "size-guard-test"


@activity.defn
async def produce(size: int) -> str:
    return "x" * size


@workflow.defn
class ProduceWorkflow:
    @workflow.run
    async def run(self, size: int) -> int:
        from datetime import timedelta

        from temporalio.common import RetryPolicy

        text = await workflow.execute_activity(
            produce,
            size,
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return len(text)


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, payload: str) -> int:
        return len(payload)


async def _run(client: Client, size: int) -> int:
    async with Worker(client, task_queue=QUEUE, workflows=[ProduceWorkflow], activities=[produce]):
        return await client.execute_workflow(
            ProduceWorkflow.run, size, id=f"wf-{uuid.uuid4()}", task_queue=QUEUE
        )


def _causes(exc: BaseException | None) -> list[BaseException]:
    chain: list[BaseException] = []
    while exc is not None:
        chain.append(exc)
        exc = exc.__cause__
    return chain


async def test_activity_result_over_the_limit_fails_the_workflow(temporal_client: Client) -> None:
    with pytest.raises(WorkflowFailureError) as info:
        await _run(temporal_client, 300 * 1024)
    chain = _causes(info.value)
    assert any(isinstance(c, ActivityError) for c in chain)
    guard = [c for c in chain if isinstance(c, ApplicationError) and c.type == "PayloadTooLarge"]
    assert guard, [repr(c) for c in chain]


async def test_activity_result_under_the_limit_completes(temporal_client: Client) -> None:
    assert await _run(temporal_client, 100 * 1024) == 100 * 1024


async def test_workflow_input_over_the_limit_is_refused_by_the_client(
    temporal_client: Client,
) -> None:
    with pytest.raises(ApplicationError) as info:
        await temporal_client.start_workflow(
            EchoWorkflow.run,
            "x" * (MAX_PAYLOAD_BYTES + 1),
            id=f"wf-{uuid.uuid4()}",
            task_queue=QUEUE,
        )
    assert info.value.type == "PayloadTooLarge"
