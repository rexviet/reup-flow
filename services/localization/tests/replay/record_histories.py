"""Records workflow histories to replay in CI.

    uv run python -m tests.replay.record_histories phase-06
    uv run python -m tests.replay.record_histories vbee-dubbing   # the Vbee workflow's set

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
from localization.providers.errors import PermanentProviderError
from localization.temporal.client import connect
from tests.support.ad_fakes import AD_LINES, AdFakes, FakeJev
from tests.support.dub_fakes import CodeDubber, DubFakes, ScriptedDubber, speak_every_line
from tests.support.inputs import make_input
from tests.support.localizer_fakes import (
    FakePorts,
    LocalizerFakes,
    OneRequestModel,
    ScriptedModel,
    segment_ids_in,
    translate_all,
)
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.review_fakes import ReviewFakes, issue, verdict
from tests.support.vbee_fakes import (
    SLOW_WORD,
    FakeVbeeStt,
    FakeVbeeVoice,
    ScriptedShortener,
    VbeeRun,
    run_vbee,
    three_slots,
    vbee_input,
    words,
)
from tests.support.voice_fakes import FakeDetector, VoiceFakes

HISTORIES = Path(__file__).parent / "histories"

# `phase-04` holds runs recorded before the Localizer stage existed and `phase-05`
# runs recorded before the dubbing stage existed. They are kept as they are: they are
# what proves a run already in flight still resumes, so those sets are never
# re-recorded.
Scenario = (
    tuple[PrepFakes, LocalizerFakes, DubFakes | None]
    | tuple[PrepFakes, LocalizerFakes, DubFakes | None, ReviewFakes]
    | tuple[PrepFakes, LocalizerFakes, DubFakes | None, ReviewFakes, AdFakes]
    | tuple[PrepFakes, LocalizerFakes, DubFakes | None, ReviewFakes, AdFakes, VoiceFakes]
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


# Answers for the agents that answer in one request: lines that fit, lines of which one is
# over its slot, and the rewrite that fixes it.
SHORT = "xin chào các bạn"
LONG = "một hai ba bốn năm sáu bảy tám chín mười một hai"


def _fits(prompt: str) -> list[dict]:
    return [{"id": i, "text_vi": SHORT} for i in segment_ids_in(prompt)]


def _one_long(prompt: str) -> list[dict]:
    return [{"id": i, "text_vi": LONG if i == 1 else SHORT} for i in segment_ids_in(prompt)]


def _with_term(prompt: str) -> list[dict]:
    return [
        {"id": i, "text_vi": "điện thoại mới" if i == 1 else SHORT} for i in segment_ids_in(prompt)
    ]


def _shorter_dub(prompt: str) -> list[dict]:
    return [{"id": i, "text_vi": "câu ngắn"} for i in (0, 1)]


SCENARIOS["single-request"] = {
    "one-request-translation": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(),
        ReviewFakes(),
    ),
    "translation-repaired-once": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_one_long, _fits))),
        DubFakes(),
        ReviewFakes(),
    ),
    "dubber-in-one-request": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(unfit_ids=(0, 1), mode="single", dubber=OneRequestModel(answers=(_shorter_dub,))),
        ReviewFakes(),
    ),
    "tool-loop-by-setting": (
        PrepFakes(),
        LocalizerFakes(mode="tools"),
        DubFakes(unfit_ids=(0, 1), mode="tools", dubber=ScriptedDubber(rewrites_per_segment=2)),
        ReviewFakes(),
    ),
}

# A line that leaves out the glossary Vietnamese of a term its source says (segment 1
# says 手机) is redone once, behind the `localizer-glossary-terms` marker.
SCENARIOS["glossary-terms"] = {
    "term-put-back-by-the-repair": (
        PrepFakes(),
        LocalizerFakes(
            mode="single",
            model=OneRequestModel(answers=(_fits, _with_term)),
            ports=FakePorts(glossary={("chan_1", "手机"): "điện thoại"}),
        ),
        DubFakes(),
        ReviewFakes(),
    ),
    "term-kept-by-the-first-answer": (
        PrepFakes(),
        LocalizerFakes(
            mode="single",
            model=OneRequestModel(answers=(_with_term,)),
            ports=FakePorts(glossary={("chan_1", "手机"): "điện thoại"}),
        ),
        DubFakes(),
        ReviewFakes(),
    ),
}


# Runs recorded with the texts to synthesize split into even batches, at least one per
# concurrent `tts` slot. Every set above is from before it: their runs cut the texts into
# runs of ten and a rest, and they prove a run already in flight still resumes.
SCENARIOS["even-tts-batches"] = {
    "three-lines-in-two-batches": (PrepFakes(), LocalizerFakes(), DubFakes(), ReviewFakes()),
    "twenty-five-lines-in-three-batches": (
        PrepFakes(),
        LocalizerFakes(),
        DubFakes(hashes=25),
        ReviewFakes(),
    ),
    "dubber-runs-again-for-one-line": (
        PrepFakes(),
        LocalizerFakes(),
        DubFakes(hashes=7),
        ReviewFakes(verdicts=[verdict(issue("dub", "rushed", segment_id=1)), verdict()]),
    ),
}


# Runs recorded with each round reviewed before the render and the video rendered once,
# after the last round. Every set above is from before it: their runs rendered each round
# and reviewed the render, and they prove a run already in flight still resumes.
SCENARIOS["review-before-render"] = {
    "published-at-the-first-review": (PrepFakes(), LocalizerFakes(), DubFakes(), ReviewFakes()),
    "dubber-runs-again-for-one-line": (
        PrepFakes(),
        LocalizerFakes(),
        DubFakes(),
        ReviewFakes(verdicts=[verdict(issue("dub", "rushed", segment_id=1)), verdict()]),
    ),
    "needs-a-person-after-two-reviews": (
        PrepFakes(),
        LocalizerFakes(),
        DubFakes(),
        ReviewFakes(
            verdicts=[
                verdict(issue("dub", "rushed", segment_id=1)),
                verdict(issue("dub", "still rushed", segment_id=1)),
            ]
        ),
    ),
    "review-could-not-be-made": (
        PrepFakes(),
        LocalizerFakes(),
        DubFakes(),
        ReviewFakes(
            fail_on={
                ("review_before_render", 1): ApplicationError(
                    "no usable answer", type="UnexpectedModelBehavior", non_retryable=True
                )
            }
        ),
    ),
    "render-fails-for-good-after-the-review": (
        PrepFakes(),
        LocalizerFakes(),
        DubFakes(),
        ReviewFakes(
            fail_on={
                ("render", 1): ApplicationError(
                    "the render ran past its deadline", type="RenderDeadline", non_retryable=True
                )
            }
        ),
    ),
    "reviewer-needs-the-render": (
        PrepFakes(),
        LocalizerFakes(),
        DubFakes(),
        ReviewFakes(
            verdicts=[verdict(issue("dub", "rushed", segment_id=1)), verdict()],
            needs_render=True,
        ),
    ),
}


# Runs recorded with the Dubber asking `dubber_setup` for its mode, behind the
# `dubber-code-mode` marker: in each of its three modes, and in `code` once with a snippet
# that fails in the middle of a gather. Every set above is from before it: their runs asked
# `dubber_context` (or nothing), and they prove a run already in flight still resumes.
SPEAK_THEN_FAIL = """\
import asyncio
await asyncio.gather(
    synthesize(id=0, text_vi="đầu 0"),
    synthesize(id=1, text_vi="đầu 1"),
    synthesize(id=99, text_vi="lạ"),
)
"""

SCENARIOS["dubber-code-mode"] = {
    "dubber-writes-code": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(unfit_ids=(0, 1), mode="code", dubber=CodeDubber()),
        ReviewFakes(),
    ),
    "code-fails-mid-gather-then-finishes": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(
            unfit_ids=(0, 1),
            mode="code",
            dubber=CodeDubber(snippets=(lambda ids: SPEAK_THEN_FAIL, speak_every_line)),
        ),
        ReviewFakes(),
    ),
    "dubber-in-one-request": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(unfit_ids=(0, 1), mode="single", dubber=OneRequestModel(answers=(_shorter_dub,))),
        ReviewFakes(),
    ),
    "tool-loop-by-setting": (
        PrepFakes(),
        LocalizerFakes(mode="tools"),
        DubFakes(unfit_ids=(0, 1), mode="tools", dubber=ScriptedDubber(rewrites_per_segment=2)),
        ReviewFakes(),
    ),
}


# Runs recorded with the question, once the video is prepared, whether it is a product
# advertisement. Every set above is from before it: their runs went from the preparation
# straight to the Localizer, and they prove a run already in flight still resumes.
SCENARIOS["skip-advertisements"] = {
    "advertisement-skipped": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(),
        ReviewFakes(),
        AdFakes(lines=AD_LINES, jev=FakeJev(score=0.99)),
    ),
    "not-an-advertisement": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(),
        ReviewFakes(),
        AdFakes(jev=FakeJev(score=0.02)),
    ),
    "check-fails-for-good": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(),
        ReviewFakes(),
        AdFakes(
            lines=AD_LINES,
            jev=FakeJev(
                fail=ApplicationError(
                    "jev: HTTP 401", type="PermanentProviderError", non_retryable=True
                )
            ),
        ),
    ),
}


# Runs recorded with the voice check before anything else: a video without a voice has its
# original published and ends there. Every set above is from before it: their runs began with
# the ingest, and they prove a run already in flight still resumes.
SCENARIOS["publish-originals-without-voice"] = {
    "original-published": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(),
        ReviewFakes(),
        AdFakes(),
        VoiceFakes(detector=FakeDetector(share=0.0)),
    ),
    "voice-localized": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(),
        ReviewFakes(),
        AdFakes(),
        VoiceFakes(detector=FakeDetector(share=0.4)),
    ),
    "check-fails-for-good": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(),
        ReviewFakes(),
        AdFakes(),
        VoiceFakes(detector=FakeDetector(share=0.0, fail=PermanentProviderError("vad"))),
    ),
    "no-video-skipped": (
        PrepFakes(),
        LocalizerFakes(mode="single", model=OneRequestModel(answers=(_fits,))),
        DubFakes(),
        ReviewFakes(),
        AdFakes(),
        VoiceFakes(source="sound_only", detector=FakeDetector(share=0.9)),
    ),
}


# Runs of the Vbee dubbing workflow, a workflow type of its own: its histories live apart
# (`vbee_histories/`), since they replay against that workflow and not the one above.
VBEE_HISTORIES = Path(__file__).parent / "vbee_histories"


def _vbee_scenarios() -> dict[str, VbeeRun]:
    slow = words(12, SLOW_WORD)
    return {
        "published-at-the-first-review": VbeeRun(
            slots=three_slots(words(10), words(30), words(10))
        ),
        "lines-shortened-at-the-measured-rate": VbeeRun(
            slots=three_slots(words(10), words(24), words(10)),
            voice=FakeVbeeVoice(rate=4.5, sample_rate=6.0),
        ),
        "review-shortens-one-line": VbeeRun(slots=three_slots(words(10), words(10), slow)),
        "needs-a-person-after-two-reviews": VbeeRun(
            slots=three_slots(words(10), words(10), slow),
            voice=FakeVbeeVoice(slow={SLOW_WORD: 2.5, "gọn": 1.0}),
        ),
        "line-that-cannot-be-shortened": VbeeRun(
            slots=three_slots(words(10), words(10), slow),
            shortener=ScriptedShortener(word="你好"),
        ),
        "no-speech": VbeeRun(slots=three_slots()),
        "stt-fails-for-good": VbeeRun(
            stt=FakeVbeeStt(PermanentProviderError("vbee stt: HTTP 401 UNAUTHORIZED"))
        ),
    }


async def record_vbee() -> None:
    VBEE_HISTORIES.mkdir(parents=True, exist_ok=True)
    env = await WorkflowEnvironment.start_local()
    try:
        client = await connect(env.client.service_client.config.target_host)
        for name, run in _vbee_scenarios().items():
            run.spec = vbee_input(str(7100 + len(list(VBEE_HISTORIES.glob("*.json")))))
            await run_vbee(client, run)
            assert run.handle is not None
            history = await run.handle.fetch_history()
            (VBEE_HISTORIES / f"{name}.json").write_text(history.to_json() + "\n")
            print(f"recorded {name}: {len(history.events)} events")
    finally:
        await env.shutdown()


async def record(phase: str) -> None:
    if phase == "vbee-dubbing":
        await record_vbee()
        return
    if phase not in SCENARIOS:
        raise SystemExit(f"nothing to record for {phase!r}; known: {sorted(SCENARIOS)}")
    out = HISTORIES / phase
    out.mkdir(parents=True, exist_ok=True)
    env = await WorkflowEnvironment.start_local()
    try:
        client = await connect(env.client.service_client.config.target_host)
        for name, (fakes, localizing, dubbing, *rest) in SCENARIOS[phase].items():
            async with prep_workers(
                client,
                fakes,
                localizing,
                dubbing=dubbing,
                reviewing=rest[0] if rest else None,
                classifying=rest[1] if len(rest) > 1 else None,
                voicing=rest[2] if len(rest) > 2 else None,
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
