"""Records workflow histories to replay in CI.

    uv run python -m tests.replay.record_histories phase-04

Each scenario is a real execution of the real workflow on a local Temporal server
(activities are the in-memory stand-ins, which is all the history needs: replay
re-executes workflow code against recorded events and never runs an activity).
Record a new set when the workflow's shape legitimately changes and the old
histories were drained or are covered by `workflow.patched`; never to make a red
replay green.
"""

import asyncio
import sys
import uuid
from pathlib import Path

from temporalio.testing import WorkflowEnvironment

from localization.contracts import PrepResult
from localization.temporal.client import connect
from tests.support.inputs import make_input
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers

HISTORIES = Path(__file__).parent / "histories"

SCENARIOS = {
    "plain-run": PrepFakes(),
    "no-burned-in-subtitles": PrepFakes(region=None),
    "lost-vocals-ingest-rerun": PrepFakes(lose_vocals_on_ingest_runs={1}),
}


async def record(phase: str) -> None:
    out = HISTORIES / phase
    out.mkdir(parents=True, exist_ok=True)
    env = await WorkflowEnvironment.start_local()
    try:
        client = await connect(env.client.service_client.config.target_host)
        for name, fakes in SCENARIOS.items():
            async with prep_workers(client, fakes):
                aweme_id = str(7000 + len(list(out.glob("*.json"))))
                handle = await client.start_workflow(
                    "VideoLocalizationWorkflow",
                    make_input(aweme_id),
                    id=f"video-{aweme_id}-g1-{uuid.uuid4().hex[:6]}",
                    task_queue="workflows",
                    result_type=PrepResult,
                )
                await asyncio.wait_for(handle.result(), timeout=60)
                history = await handle.fetch_history()
            (out / f"{name}.json").write_text(history.to_json() + "\n")
            print(f"recorded {name}: {len(history.events)} events")
    finally:
        await env.shutdown()


if __name__ == "__main__":
    asyncio.run(record(sys.argv[1] if len(sys.argv) > 1 else "phase-04"))
