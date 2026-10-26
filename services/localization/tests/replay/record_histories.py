"""Records workflow histories to replay in CI.

    uv run python -m tests.replay.record_histories phase-05

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

from localization.contracts import LocalizationResult
from localization.temporal.client import connect
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes, ScriptedModel, translate_all
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers

HISTORIES = Path(__file__).parent / "histories"

# `phase-04` holds runs recorded before the Localizer stage existed. They are kept
# as they are: they are what proves a run already in flight still resumes, so that
# set is never re-recorded.
SCENARIOS: dict[str, dict[str, tuple[PrepFakes, LocalizerFakes]]] = {
    "phase-05": {
        "plain-run": (PrepFakes(), LocalizerFakes()),
        "no-burned-in-subtitles": (PrepFakes(region=None), LocalizerFakes()),
        "lost-vocals-ingest-rerun": (PrepFakes(lose_vocals_on_ingest_runs={1}), LocalizerFakes()),
        "glossary-and-estimate-tools": (
            PrepFakes(),
            LocalizerFakes(
                model=ScriptedModel(
                    tool_calls=(
                        ("read_transcript", {}),
                        ("glossary_lookup", {"terms": ["手机"]}),
                        ("estimate_duration", {"texts": ["xin chào"]}),
                        ("glossary_upsert", {"term_zh": "手机", "term_vi": "điện thoại"}),
                    )
                )
            ),
        ),
        "answer-rejected-then-fixed": (
            PrepFakes(),
            LocalizerFakes(
                model=ScriptedModel(answers=(lambda ids: translate_all(ids[:-1]), translate_all))
            ),
        ),
    }
}


async def record(phase: str) -> None:
    if phase not in SCENARIOS:
        raise SystemExit(f"nothing to record for {phase!r}; known: {sorted(SCENARIOS)}")
    out = HISTORIES / phase
    out.mkdir(parents=True, exist_ok=True)
    env = await WorkflowEnvironment.start_local()
    try:
        client = await connect(env.client.service_client.config.target_host)
        for name, (fakes, localizing) in SCENARIOS[phase].items():
            async with prep_workers(client, fakes, localizing):
                aweme_id = str(7000 + len(list(out.glob("*.json"))))
                handle = await client.start_workflow(
                    "VideoLocalizationWorkflow",
                    make_input(aweme_id),
                    id=f"video-{aweme_id}-g1-{uuid.uuid4().hex[:6]}",
                    task_queue="workflows",
                    result_type=LocalizationResult,
                )
                await asyncio.wait_for(handle.result(), timeout=60)
                history = await handle.fetch_history()
            (out / f"{name}.json").write_text(history.to_json() + "\n")
            print(f"recorded {name}: {len(history.events)} events")
    finally:
        await env.shutdown()


if __name__ == "__main__":
    asyncio.run(record(sys.argv[1] if len(sys.argv) > 1 else "phase-05"))
