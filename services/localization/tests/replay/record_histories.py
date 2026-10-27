"""Records workflow histories to replay in CI.

    uv run python -m tests.replay.record_histories phase-06

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

from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from localization.contracts import LocalizationResult
from localization.temporal.client import connect
from tests.support.dub_fakes import DubFakes, ScriptedDubber
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes, ScriptedModel, translate_all
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.review_fakes import ReviewFakes, issue, verdict

HISTORIES = Path(__file__).parent / "histories"

# `phase-04` holds runs recorded before the Localizer stage existed and `phase-05`
# runs recorded before the dubbing stage existed. They are kept as they are: they are
# what proves a run already in flight still resumes, so those sets are never
# re-recorded.
Scenario = (
    tuple[PrepFakes, LocalizerFakes, DubFakes | None]
    | tuple[PrepFakes, LocalizerFakes, DubFakes | None, ReviewFakes]
)
SCENARIOS: dict[str, dict[str, Scenario]] = {
    "phase-05": {
        "plain-run": (PrepFakes(), LocalizerFakes(), None),
        "no-burned-in-subtitles": (PrepFakes(region=None), LocalizerFakes(), None),
        "lost-vocals-ingest-rerun": (
            PrepFakes(lose_vocals_on_ingest_runs={1}),
            LocalizerFakes(),
            None,
        ),
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
            None,
        ),
        "answer-rejected-then-fixed": (
            PrepFakes(),
            LocalizerFakes(
                model=ScriptedModel(answers=(lambda ids: translate_all(ids[:-1]), translate_all))
            ),
            None,
        ),
    },
    "phase-06": {
        "plain-run": (PrepFakes(), LocalizerFakes(), DubFakes(hashes=25)),
        "no-burned-in-subtitles": (PrepFakes(region=None), LocalizerFakes(), DubFakes()),
        "lost-vocals-ingest-rerun": (
            PrepFakes(lose_vocals_on_ingest_runs={1}),
            LocalizerFakes(),
            DubFakes(),
        ),
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
            DubFakes(),
        ),
        "answer-rejected-then-fixed": (
            PrepFakes(),
            LocalizerFakes(
                model=ScriptedModel(answers=(lambda ids: translate_all(ids[:-1]), translate_all))
            ),
            DubFakes(),
        ),
        "dubber-shortens-lines": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(unfit_ids=(0, 1), dubber=ScriptedDubber(rewrites_per_segment=2)),
        ),
        "music-stem-lost-ingest-rerun": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(missing_on={("assemble_audio", 1): "ingest"}),
        ),
        "script-lost-localizer-rerun": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(missing_on={("measure_fit", 1): "localize"}),
        ),
    },
    # Runs recorded with the layout, render, review and publish steps. `phase-06` holds
    # runs from before them, and is what proves one already in flight still resumes.
    "phase-08": {
        "published-at-the-first-review": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(),
            ReviewFakes(),
        ),
        "dubber-runs-again-for-one-line": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(),
            ReviewFakes(verdicts=[verdict(issue("dub", "rushed", segment_id=1)), verdict()]),
        ),
        "localizer-runs-again-with-the-notes": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(),
            ReviewFakes(
                verdicts=[verdict(issue("localize", "too formal", segment_id=0)), verdict()]
            ),
        ),
        "layout-runs-again": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(),
            ReviewFakes(
                verdicts=[verdict(issue("captions", "cut off")), verdict()],
                changed_layout_in_rounds={1},
            ),
        ),
        "needs-a-person-after-two-reviews": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(unfit_ids=(0, 1), dubber=ScriptedDubber(rewrites_per_segment=2)),
            ReviewFakes(
                verdicts=[
                    verdict(issue("dub", "rushed", segment_id=1)),
                    verdict(issue("dub", "still rushed", segment_id=1)),
                ]
            ),
        ),
        "flagged-translation-needs-a-person": (
            PrepFakes(region=None),
            LocalizerFakes(flagged_ids=(0,)),
            DubFakes(),
            ReviewFakes(),
        ),
        "layout-fails-for-good": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(),
            ReviewFakes(
                fail_on={
                    ("layout_captions", 1): ApplicationError(
                        "the caption font has no glyph for segment 0: 你",
                        type="UncoveredGlyphs",
                        non_retryable=True,
                    )
                }
            ),
        ),
    },
    # Runs recorded with the question "could a rerun fix this?" asked before another
    # round. `phase-08` holds runs from before it, and is what proves one already in
    # flight still resumes.
    "unfixable-review": {
        "last-line-has-no-time": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(),
            ReviewFakes(
                verdicts=[
                    verdict(
                        issue("localize", "the meaning was lost", segment_id=1),
                        issue("dub", "overruns a 1000 ms slot", segment_id=1),
                        issue("captions", "visible for 0.3 s", segment_id=1),
                    )
                ],
                tight={1: (402, 1000)},
            ),
        ),
        "middle-line-runs-again": (
            PrepFakes(),
            LocalizerFakes(),
            DubFakes(),
            ReviewFakes(
                verdicts=[verdict(issue("dub", "rushed", segment_id=0)), verdict()],
                tight={1: (402, 1000)},
            ),
        ),
    },
    # Runs recorded with the voice's rate measured beside the ingest. Every set above is
    # from before it, and is what proves a run already in flight still resumes.
    "voice-rate-with-ingest": {
        "plain-run": (PrepFakes(), LocalizerFakes(), DubFakes(), ReviewFakes()),
        "rate-outlasts-the-preparation": (
            PrepFakes(),
            LocalizerFakes(rate_seconds=2.0),
            DubFakes(),
            ReviewFakes(),
        ),
        "lost-vocals-ingest-rerun": (
            PrepFakes(lose_vocals_on_ingest_runs={1}, delay_seconds=0.3),
            LocalizerFakes(rate_seconds=1.0),
            DubFakes(),
            ReviewFakes(),
        ),
        "voice-rate-fails-for-good": (
            PrepFakes(ingest_seconds=3.0),
            LocalizerFakes(
                rate_error=ApplicationError(
                    "the voice is not known to the provider",
                    type="PermanentProviderError",
                    non_retryable=True,
                )
            ),
            DubFakes(),
            ReviewFakes(),
        ),
    },
}


async def record(phase: str) -> None:
    if phase not in SCENARIOS:
        raise SystemExit(f"nothing to record for {phase!r}; known: {sorted(SCENARIOS)}")
    out = HISTORIES / phase
    out.mkdir(parents=True, exist_ok=True)
    env = await WorkflowEnvironment.start_local()
    try:
        client = await connect(env.client.service_client.config.target_host)
        for name, (fakes, localizing, dubbing, *rest) in SCENARIOS[phase].items():
            async with prep_workers(
                client, fakes, localizing, dubbing=dubbing, reviewing=rest[0] if rest else None
            ):
                aweme_id = str(7000 + len(list(out.glob("*.json"))))
                handle = await client.start_workflow(
                    "VideoLocalizationWorkflow",
                    make_input(aweme_id),
                    id=f"video-{aweme_id}-g1-{uuid.uuid4().hex[:6]}",
                    task_queue="workflows",
                    result_type=LocalizationResult,
                )
                try:
                    await asyncio.wait_for(handle.result(), timeout=60)
                except WorkflowFailureError:
                    # A run that ends in failure is a history worth replaying too.
                    pass
                history = await handle.fetch_history()
            (out / f"{name}.json").write_text(history.to_json() + "\n")
            print(f"recorded {name}: {len(history.events)} events")
    finally:
        await env.shutdown()


if __name__ == "__main__":
    asyncio.run(record(sys.argv[1] if len(sys.argv) > 1 else "phase-08"))
