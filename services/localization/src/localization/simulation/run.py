"""Run a credential-free mock worker against a dedicated real Temporal namespace."""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack

from google.protobuf.duration_pb2 import Duration
from temporalio import activity
from temporalio.api.workflowservice.v1 import (
    DescribeNamespaceRequest,
    RegisterNamespaceRequest,
)
from temporalio.client import Client
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from localization.simulation.workflow import NAMESPACE, QUEUE, STEPS, WorkspaceSimulationWorkflow

SCENARIOS = {
    "success": "9900000000000000001",
    "retry": "9900000000000000002",
    "failure": "9900000000000000003",
}


def mock_activity(name: str):
    @activity.defn(name=name)
    async def run(scenario: str, seconds: float) -> str:
        info = activity.info()
        logging.info("MOCK %s scenario=%s attempt=%s", name, scenario, info.attempt)
        await asyncio.sleep(seconds)
        if scenario == "retry" and name == "synthesize_segments" and info.attempt < 3:
            raise ApplicationError("Simulated transient error", type="SimulationRetry")
        if scenario == "failure" and name == "review_before_render":
            raise ApplicationError(
                "Simulated review failure", type="SimulationFailure", non_retryable=True
            )
        return "mock-complete"

    return run


async def serve(address: str, seconds: float) -> None:
    client = await Client.connect(address, namespace=NAMESPACE)
    try:
        await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=NAMESPACE)
        )
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        try:
            await client.workflow_service.register_namespace(
                RegisterNamespaceRequest(
                    namespace=NAMESPACE,
                    description="Workspace mock activities only; no production side effects",
                    workflow_execution_retention_period=Duration(seconds=86400),
                )
            )
        except RPCError as register_exc:
            if register_exc.status != RPCStatusCode.ALREADY_EXISTS:
                raise

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            Worker(client, task_queue=QUEUE, workflows=[WorkspaceSimulationWorkflow])
        )
        for queue in sorted({queue for _, queue in STEPS}):
            await stack.enter_async_context(
                Worker(
                    client,
                    task_queue=queue,
                    activities=[
                        mock_activity(name) for name, q in sorted(set(STEPS)) if q == queue
                    ],
                )
            )
        for scenario, aweme_id in SCENARIOS.items():
            # Namespace registration propagates asynchronously to history hosts.
            for attempt in range(30):
                try:
                    await client.start_workflow(
                        WorkspaceSimulationWorkflow.run,
                        args=[scenario, seconds],
                        id=f"video-{aweme_id}-g1",
                        task_queue=QUEUE,
                    )
                    break
                except WorkflowAlreadyStartedError:
                    break
                except RPCError as exc:
                    if exc.status != RPCStatusCode.NOT_FOUND or attempt == 29:
                        raise
                    await asyncio.sleep(2)
            logging.info("Simulation ready: %s video-%s-g1", scenario, aweme_id)
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="temporal:7233")
    parser.add_argument("--step-seconds", type=float, default=6)
    args = parser.parse_args()
    if not 1 <= args.step_seconds <= 60:
        parser.error("--step-seconds must be between 1 and 60")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(args.address, args.step_seconds))


if __name__ == "__main__":
    main()
