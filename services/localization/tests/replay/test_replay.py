"""Replays recorded histories against the current workflow code.

A run that is already in flight when new workflow code is deployed resumes by
replaying its history. If the code now issues its commands in a different order,
the replay fails and the run is stuck. Every history recorded under
`histories/` must keep replaying; a change that reorders commands must be guarded
with `workflow.patched("<name>")` so old histories still take their old path.
"""

import base64
import json
from pathlib import Path

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError

from localization.temporal.client import guarded_data_converter
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.replay.ungated_ad_gate_flow import UngatedAdGateWorkflow
from tests.replay.ungated_batches_flow import UngatedBatchesWorkflow
from tests.replay.ungated_review_first_flow import UngatedReviewFirstWorkflow
from tests.replay.ungated_voice_check_flow import UngatedVoiceCheckWorkflow
from tests.support.reordered_flow import ReorderedWorkflow
from tests.support.ungated_dub_flow import UngatedDubWorkflow
from tests.support.ungated_rate_flow import UngatedRateWorkflow
from tests.support.ungated_review_flow import UngatedReviewWorkflow
from tests.support.ungated_route_flow import UngatedRouteWorkflow
from tests.support.ungated_single_flow import UngatedSingleWorkflow

HISTORIES = sorted((Path(__file__).parent / "histories").glob("*/*.json"))


def _replayer(workflow: type) -> Replayer:
    return Replayer(
        workflows=[workflow],
        data_converter=guarded_data_converter(),
        plugins=[PydanticAIPlugin()],
    )


def _history(path: Path) -> WorkflowHistory:
    return WorkflowHistory.from_json(f"replay-{path.stem}", path.read_text())


def _patches(path: Path) -> set[str]:
    """Ids of the `workflow.patched()` markers recorded in a history."""
    found: set[str] = set()
    for event in json.loads(path.read_text())["events"]:
        marker = event.get("markerRecordedEventAttributes")
        if marker and marker["markerName"] == "core_patch":
            payload = marker["details"]["patch-data"]["payloads"][0]["data"]
            found.add(json.loads(base64.b64decode(payload))["id"])
    return found


def _of(phase: str) -> list[Path]:
    return [p for p in HISTORIES if p.parent.name == phase]


def test_there_are_histories_to_replay() -> None:
    """An empty directory would make every replay test below pass vacuously."""
    for phase in ("phase-04", "phase-05", "phase-06"):
        names = {p.stem for p in _of(phase)}
        assert {"plain-run", "no-burned-in-subtitles", "lost-vocals-ingest-rerun"} <= names, phase
    for phase in ("phase-05", "phase-06"):
        assert {"glossary-and-estimate-tools", "answer-rejected-then-fixed"} <= {
            p.stem for p in _of(phase)
        }
    assert {
        "published-at-the-first-review",
        "dubber-runs-again-for-one-line",
        "localizer-runs-again-with-the-notes",
        "layout-runs-again",
        "needs-a-person-after-two-reviews",
        "flagged-translation-needs-a-person",
        "layout-fails-for-good",
    } <= {p.stem for p in _of("phase-08")}
    assert {
        "dubber-shortens-lines",
        "music-stem-lost-ingest-rerun",
        "script-lost-localizer-rerun",
    } <= {p.stem for p in _of("phase-06")}


def test_the_histories_recorded_before_the_localizer_stage_are_really_without_it() -> None:
    """They are the proof that a run already in flight when the stage shipped still
    replays; a recording that already had the stage would prove nothing."""
    for path in _of("phase-04"):
        text = path.read_text()
        assert "voice_rate" not in text, path.name
        assert "localize-script" not in _patches(path), path.name


def test_the_histories_recorded_before_the_dubbing_stage_are_really_without_it() -> None:
    """The proof that a run already in flight when dubbing shipped still replays."""
    for path in _of("phase-04") + _of("phase-05"):
        text = path.read_text()
        assert "voice_profile" not in text and "synthesize_segments" not in text, path.name
        assert "dub-audio" not in _patches(path), path.name


def test_the_histories_recorded_before_the_review_steps_are_really_without_them() -> None:
    """The proof that a run already in flight when the review shipped still replays."""
    for path in _of("phase-04") + _of("phase-05") + _of("phase-06"):
        text = path.read_text()
        for step in ("layout_captions", "review_render", '"publish"'):
            assert step not in text, (path.name, step)
        assert "review-publish" not in _patches(path), path.name


def test_the_histories_recorded_with_the_review_steps_have_them() -> None:
    for path in _of("phase-08"):
        text = path.read_text()
        assert {"localize-script", "dub-audio", "review-publish"} <= _patches(path), path.name
        # A run that fails in the layout never gets as far as the render.
        steps = ("layout_captions", "publish")
        if path.stem != "layout-fails-for-good":
            steps = (*steps, '"render"', "review_render")
        for step in steps:
            assert step in text, (path.name, step)


def test_the_histories_of_a_rerun_show_the_stage_that_ran_again() -> None:
    """A set of histories that never re-ran anything would prove the loop replays
    without ever having been in it. A review is one scheduled activity, which names
    itself once."""
    by = {p.stem: p.read_text() for p in _of("phase-08")}
    assert by["published-at-the-first-review"].count("review_render") == 1
    for name in (
        "dubber-runs-again-for-one-line",
        "localizer-runs-again-with-the-notes",
        "layout-runs-again",
        "needs-a-person-after-two-reviews",
    ):
        assert by[name].count("review_render") == 2, name
    assert by["layout-fails-for-good"].count("review_render") == 0
    assert "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED" in by["layout-fails-for-good"]


def test_the_histories_recorded_with_the_dubbing_stage_have_it() -> None:
    for path in _of("phase-06"):
        text = path.read_text()
        assert {"localize-script", "dub-audio"} <= _patches(path), path.name
        for activity in ("voice_profile", "synthesize_segments", "commit_dub", "assemble_audio"):
            assert activity in text, (path.name, activity)


def test_the_histories_recorded_with_the_localizer_stage_have_it() -> None:
    for path in _of("phase-05"):
        text = path.read_text()
        assert "localize-script" in _patches(path), path.name
        assert "agent__localizer__model_request" in text, path.name
        assert "assemble_script" in text, path.name


@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_every_recorded_history_still_replays(path: Path) -> None:
    await _replayer(VideoLocalizationWorkflow).replay_workflow(_history(path))


@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_control_a_reordered_workflow_fails_to_replay(path: Path) -> None:
    """The replay is only a guard if it can fail: the same histories against a
    workflow that starts 2b before 2a must be rejected."""
    with pytest.raises(NondeterminismError):
        await _replayer(ReorderedWorkflow).replay_workflow(_history(path))


@pytest.mark.parametrize("path", _of("phase-05"), ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_control_the_dubbing_stage_without_its_marker_breaks_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded before the stage replay: the same stage
    added without it must be rejected, or the real workflow's marker proves nothing.
    (Histories from before the Localizer stage end at its own marker, earlier.)"""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedDubWorkflow).replay_workflow(_history(path))


@pytest.mark.parametrize("path", _of("phase-06"), ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_control_the_review_steps_without_their_marker_break_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded before the review steps replay: the same
    steps added without it must be rejected, or the real workflow's marker proves
    nothing."""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedReviewWorkflow).replay_workflow(_history(path))


def test_the_histories_recorded_with_the_rerun_question_have_it() -> None:
    names = {p.stem for p in _of("unfixable-review")}
    assert {"last-line-has-no-time", "middle-line-runs-again"} <= names
    for path in _of("unfixable-review"):
        assert "stop-unfixable-review" in _patches(path), path.name
        assert "segment_room" in path.read_text(), path.name
    by = {p.stem: p.read_text() for p in _of("unfixable-review")}
    # The run whose last line has no time ends after one review; the other runs again.
    assert by["last-line-has-no-time"].count("review_render") == 1
    assert by["middle-line-runs-again"].count("review_render") == 2


def test_the_histories_recorded_before_the_rerun_question_are_really_without_it() -> None:
    for path in _of("phase-08"):
        assert "segment_room" not in path.read_text(), path.name
        assert "stop-unfixable-review" not in _patches(path), path.name


RERAN = (
    "dubber-runs-again-for-one-line",
    "localizer-runs-again-with-the-notes",
    "layout-runs-again",
    "needs-a-person-after-two-reviews",
)


@pytest.mark.parametrize(
    "path", [p for p in _of("phase-08") if p.stem in RERAN], ids=lambda p: p.stem
)
async def test_control_the_rerun_question_without_its_marker_breaks_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded before the question replay: the same
    question asked without it must be rejected, or the real workflow's marker proves
    nothing."""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedRouteWorkflow).replay_workflow(_history(path))


NEW_RATE = "voice-rate-with-ingest"
ONE_REQUEST = "single-request"
GLOSSARY_TERMS = "glossary-terms"
EVEN_TTS = "even-tts-batches"
REVIEW_FIRST = "review-before-render"
CODE_MODE = "dubber-code-mode"
AD_GATE = "skip-advertisements"
VOICE_CHECK = "publish-originals-without-voice"


def _scheduled(path: Path) -> list[str]:
    """The activities a history scheduled, in order."""
    return [
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in json.loads(path.read_text())["events"]
        if "activityTaskScheduledEventAttributes" in e
    ]


def test_the_histories_recorded_with_the_rate_beside_the_ingest_have_it() -> None:
    by = {p.stem: _scheduled(p) for p in _of(NEW_RATE)}
    assert {
        "plain-run",
        "rate-outlasts-the-preparation",
        "lost-vocals-ingest-rerun",
        "voice-rate-fails-for-good",
    } <= by.keys()
    for path in _of(NEW_RATE):
        assert "voice-rate-with-ingest" in _patches(path), path.name
    # The rate is asked for once, right after the ingest is, and not again by the
    # Localizer stage.
    for name, scheduled in by.items():
        assert scheduled.count("voice_rate") == 1, name
        assert scheduled[:2] == ["ingest", "voice_rate"], name
    assert by["lost-vocals-ingest-rerun"].count("ingest") == 2
    failed = _of(NEW_RATE)[-1].read_text()
    assert _of(NEW_RATE)[-1].stem == "voice-rate-fails-for-good"
    assert "EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED" in failed
    assert "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED" in failed


def test_the_histories_recorded_before_the_rate_moved_are_really_without_it() -> None:
    """The rate was asked for after the preparation in every one of them (or not at all,
    before the Localizer stage existed)."""
    for path in HISTORIES:
        if path.parent.name in (
            NEW_RATE,
            ONE_REQUEST,
            GLOSSARY_TERMS,
            EVEN_TTS,
            REVIEW_FIRST,
            CODE_MODE,
            AD_GATE,
            VOICE_CHECK,
        ):
            continue
        assert "voice-rate-with-ingest" not in _patches(path), path.name
        scheduled = _scheduled(path)
        if "voice_rate" in scheduled:
            assert scheduled.index("voice_rate") > scheduled.index("detect_subtitles"), path.name


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in HISTORIES
        if p.parent.name not in (NEW_RATE, ONE_REQUEST, CODE_MODE)
        # A run that ended at the voice check never prepared anything, so had no rate to move.
        and not (
            p.parent.name == VOICE_CHECK and p.stem in ("original-published", "no-video-skipped")
        )
    ],
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
async def test_control_the_rate_beside_the_ingest_without_its_marker_breaks_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded before the rate moved replay: the same move
    made without it must be rejected, or the real workflow's marker proves nothing."""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedRateWorkflow).replay_workflow(_history(path))


def test_the_histories_recorded_with_the_one_request_agents_have_them() -> None:
    by = {p.stem: p for p in _of(ONE_REQUEST)}
    assert {
        "one-request-translation",
        "translation-repaired-once",
        "dubber-in-one-request",
        "tool-loop-by-setting",
    } == by.keys()
    for path in by.values():
        assert "localizer-single-request" in _patches(path), path.name
        assert "localizer_context" in _scheduled(path), path.name
    single = _scheduled(by["one-request-translation"])
    assert single.count("agent__localizer_single__model_request") == 1
    assert "agent__localizer_repair__model_request" not in single
    assert "agent__localizer__model_request" not in single
    repaired = _scheduled(by["translation-repaired-once"])
    assert repaired.count("agent__localizer_single__model_request") == 1
    assert repaired.count("agent__localizer_repair__model_request") == 1
    dubbed = by["dubber-in-one-request"]
    assert "dubber-single-request" in _patches(dubbed)
    assert _scheduled(dubbed).count("agent__dubber_single__model_request") == 1
    # The setting that keeps the loop is recorded too: the marker is there, the context
    # activity answered "no", and the loop's own requests follow.
    loop = _scheduled(by["tool-loop-by-setting"])
    assert "agent__localizer__model_request" in loop and "agent__dubber__model_request" in loop
    assert not any("single" in name or "repair" in name for name in loop)


def test_the_histories_recorded_before_the_one_request_agents_are_really_without_them() -> None:
    for path in HISTORIES:
        if path.parent.name in (
            ONE_REQUEST,
            GLOSSARY_TERMS,
            EVEN_TTS,
            REVIEW_FIRST,
            CODE_MODE,
            AD_GATE,
            VOICE_CHECK,
        ):
            continue
        assert "localizer-single-request" not in _patches(path), path.name
        assert "dubber-single-request" not in _patches(path), path.name
        assert "localizer_context" not in _scheduled(path), path.name
        assert "dubber_context" not in _scheduled(path), path.name


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in HISTORIES
        # A run that ends before the Localizer stage never reaches the context.
        if p.parent.name not in ("phase-04", ONE_REQUEST, CODE_MODE)
        and p.stem
        not in (
            "voice-rate-fails-for-good",
            "advertisement-skipped",
            "original-published",
            "no-video-skipped",
        )
    ],
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
async def test_control_the_localizer_context_without_its_marker_breaks_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded before the one-request mode replay the tool
    loop: the same context read without it must be rejected, or the real workflow's marker
    proves nothing. (Histories from before the Localizer stage end at its own marker.)"""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedSingleWorkflow).replay_workflow(_history(path))


def test_the_glossary_term_histories_carry_the_check_and_one_of_them_its_repair() -> None:
    """The check's histories must really be on its path: the marker is recorded, and the
    run whose first answer left the term out made the repair request."""
    by = {p.stem: p for p in _of(GLOSSARY_TERMS)}
    assert set(by) == {"term-put-back-by-the-repair", "term-kept-by-the-first-answer"}
    for path in by.values():
        assert "localizer-glossary-terms" in _patches(path), path.stem
    assert "agent__localizer_repair__model_request" in by["term-put-back-by-the-repair"].read_text()
    assert "agent__localizer_repair__model_request" not in (
        by["term-kept-by-the-first-answer"].read_text()
    )
    # Every history recorded before the check runs without it.
    older = [
        p
        for p in HISTORIES
        if p.parent.name not in (GLOSSARY_TERMS, REVIEW_FIRST, CODE_MODE, AD_GATE, VOICE_CHECK)
    ]
    assert older and not any("localizer-glossary-terms" in _patches(p) for p in older)


def test_the_even_batch_histories_carry_the_marker_and_one_batch_per_slot() -> None:
    """The split's histories must really be on its path: the marker is recorded, and a
    script of three lines (one batch before) was spoken as two."""
    by = {p.stem: _scheduled(p) for p in _of(EVEN_TTS)}
    assert set(by) == {
        "three-lines-in-two-batches",
        "twenty-five-lines-in-three-batches",
        "dubber-runs-again-for-one-line",
    }
    for path in _of(EVEN_TTS):
        assert "even-tts-batches" in _patches(path), path.stem
    # Each run speaks its script twice (the Localizer's, then the dubbed one); the rerun
    # after the review speaks it a third time.
    assert by["three-lines-in-two-batches"].count("synthesize_segments") == 2 * 2
    assert by["twenty-five-lines-in-three-batches"].count("synthesize_segments") == 2 * 3
    assert by["dubber-runs-again-for-one-line"].count("synthesize_segments") == 3 * 2
    assert by["dubber-runs-again-for-one-line"].count("review_render") == 2


def test_the_histories_recorded_before_the_even_batches_are_really_without_them() -> None:
    older = [
        p
        for p in HISTORIES
        if p.parent.name not in (EVEN_TTS, REVIEW_FIRST, CODE_MODE, AD_GATE, VOICE_CHECK)
    ]
    assert older and not any("even-tts-batches" in _patches(p) for p in older)


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in HISTORIES
        if p.parent.name not in (EVEN_TTS, REVIEW_FIRST, CODE_MODE)
        and "synthesize_segments" in _scheduled(p)
        # Twenty-five lines are three batches either way, and replay does not compare an
        # activity's input, so that run cannot tell the two splits apart.
        and (p.parent.name, p.stem) != ("phase-06", "plain-run")
    ],
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
async def test_control_the_even_batches_without_their_marker_break_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded before the split replay: the same split made
    without it must be rejected, or the real workflow's marker proves nothing."""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedBatchesWorkflow).replay_workflow(_history(path))


def _rounds_of(path: Path) -> list[str]:
    """What a history scheduled from the first layout on."""
    scheduled = _scheduled(path)
    return scheduled[scheduled.index("layout_captions") :]


def test_the_review_first_histories_carry_the_marker_and_render_once_after_the_review() -> None:
    """The new order's histories must really be on its path: the marker is recorded, every
    round is reviewed without a render, and the one render follows the last review."""
    by = {p.stem: _rounds_of(p) for p in _of(REVIEW_FIRST)}
    assert set(by) == {
        "published-at-the-first-review",
        "dubber-runs-again-for-one-line",
        "needs-a-person-after-two-reviews",
        "review-could-not-be-made",
        "render-fails-for-good-after-the-review",
        "reviewer-needs-the-render",
    }
    for path in _of(REVIEW_FIRST):
        assert REVIEW_FIRST in _patches(path), path.stem
    assert by["published-at-the-first-review"] == [
        "layout_captions",
        "review_before_render",
        "render",
        "publish",
    ]
    for name in ("dubber-runs-again-for-one-line", "needs-a-person-after-two-reviews"):
        rounds = by[name]
        assert rounds.count("review_before_render") == 2 and rounds.count("render") == 1, name
        assert "review_render" not in rounds, name
        assert rounds[-2:] == ["render", "publish"], name
    # A review that could not be made still has the video rendered, for the person.
    assert by["review-could-not-be-made"] == [
        "layout_captions",
        "review_before_render",
        "render",
        "publish",
    ]
    failed = by["render-fails-for-good-after-the-review"]
    assert failed == ["layout_captions", "review_before_render", "render", "publish"]
    text = {p.stem: p.read_text() for p in _of(REVIEW_FIRST)}
    assert "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED" in text["render-fails-for-good-after-the-review"]
    # The reviewer that looks at frames is shown a render of each round, as before.
    frames = by["reviewer-needs-the-render"]
    assert frames.count("render") == frames.count("review_render") == 2
    assert frames[:4] == ["layout_captions", "review_before_render", "render", "review_render"]


def test_the_histories_recorded_before_the_review_came_first_are_really_without_it() -> None:
    older = [
        p for p in HISTORIES if p.parent.name not in (REVIEW_FIRST, CODE_MODE, AD_GATE, VOICE_CHECK)
    ]
    assert older and not any(REVIEW_FIRST in _patches(p) for p in older)
    assert not any("review_before_render" in _scheduled(p) for p in older)


@pytest.mark.parametrize(
    "path",
    # A run that never had a round reviewed (it failed earlier) issues the same commands
    # either way.
    [
        p
        for p in HISTORIES
        if p.parent.name not in (REVIEW_FIRST, CODE_MODE) and "review_render" in _scheduled(p)
    ],
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
async def test_control_the_review_first_without_its_marker_breaks_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded when every round was rendered replay: the same
    order taken without it must be rejected, or the real workflow's marker proves nothing."""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedReviewFirstWorkflow).replay_workflow(_history(path))


CODE_TOOL = "agent__dubber_code__toolset__<agent>__call_tool"


def _scheduled_on(path: Path) -> list[tuple[str, str]]:
    """The activities a history scheduled, with the queue each was sent to."""
    return [
        (
            e["activityTaskScheduledEventAttributes"]["activityType"]["name"],
            e["activityTaskScheduledEventAttributes"]["taskQueue"]["name"],
        )
        for e in json.loads(path.read_text())["events"]
        if "activityTaskScheduledEventAttributes" in e
    ]


def test_the_code_mode_histories_ask_the_setup_and_each_runs_its_mode() -> None:
    """The set must really be on the new path in each of the three modes: the marker is
    recorded, the Dubber's mode is asked through `dubber_setup` (not `dubber_context`), and
    the agent of that mode is the one that ran."""
    by = {p.stem: p for p in _of(CODE_MODE)}
    assert set(by) == {
        "dubber-writes-code",
        "code-fails-mid-gather-then-finishes",
        "dubber-in-one-request",
        "tool-loop-by-setting",
    }
    for path in by.values():
        assert CODE_MODE in _patches(path), path.stem
        scheduled = _scheduled(path)
        assert scheduled.count("dubber_setup") == 1, path.stem
        assert "dubber_context" not in scheduled, path.stem
        assert "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED" in path.read_text(), path.stem
    for name in ("dubber-writes-code", "code-fails-mid-gather-then-finishes"):
        on = _scheduled_on(by[name])
        assert not any(n.startswith("agent__dubber__") for n, _ in on), name
        assert ("agent__dubber_code__model_request", "llm") in on, name
        assert (CODE_TOOL, "tts") in on and (CODE_TOOL, "io") in on, name
    # Two lines read and spoken, each speech its own activity on `tts`.
    plain = _scheduled_on(by["dubber-writes-code"])
    assert plain.count((CODE_TOOL, "tts")) == 2 and plain.count((CODE_TOOL, "io")) == 2
    # The failed snippet asked for one more model request, and its two speeches were made.
    failed = _scheduled_on(by["code-fails-mid-gather-then-finishes"])
    assert failed.count(("agent__dubber_code__model_request", "llm")) == 3
    assert failed.count((CODE_TOOL, "tts")) >= 4
    single = _scheduled(by["dubber-in-one-request"])
    assert single.count("agent__dubber_single__model_request") == 1
    assert not any(n.startswith("agent__dubber_code__") for n in single)
    loop = _scheduled(by["tool-loop-by-setting"])
    assert "agent__dubber__model_request" in loop
    assert not any(n.startswith("agent__dubber_code__") for n in loop)


def test_the_histories_recorded_before_the_code_mode_are_really_without_it() -> None:
    """They asked `dubber_context` or nothing: the proof that a run already in flight when
    the code mode shipped still replays."""
    older = [p for p in HISTORIES if p.parent.name not in (CODE_MODE, VOICE_CHECK)]
    assert older and not any(CODE_MODE in _patches(p) for p in older)
    assert not any("dubber_setup" in _scheduled(p) for p in older)
    assert any("dubber_context" in _scheduled(p) for p in older)
    assert not any("agent__dubber_code__" in p.read_text() for p in older)


def test_the_ad_check_histories_carry_the_marker_and_one_ends_after_the_preparation() -> None:
    """The set must really be on the new path: the marker is recorded, the check is asked
    once, right after the preparation, and the advertisement ends there with its row."""
    by = {p.stem: p for p in _of(AD_GATE)}
    assert set(by) == {"advertisement-skipped", "not-an-advertisement", "check-fails-for-good"}
    for path in by.values():
        assert AD_GATE in _patches(path), path.stem
        scheduled = _scheduled(path)
        assert scheduled.count("classify_video") == 1, path.stem
        before = scheduled[: scheduled.index("classify_video")]
        assert set(before) == {"ingest", "voice_rate", "transcribe", "detect_subtitles"}, path.stem
        assert "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED" in path.read_text(), path.stem
    skipped = _scheduled(by["advertisement-skipped"])
    assert skipped[-2:] == ["classify_video", "publish"]
    (publish,) = [
        e["activityTaskScheduledEventAttributes"]["input"]["payloads"][1]["data"]
        for e in json.loads(by["advertisement-skipped"].read_text())["events"]
        if e.get("activityTaskScheduledEventAttributes", {}).get("activityType", {}).get("name")
        == "publish"
    ]
    assert json.loads(base64.b64decode(publish))["outcome"] == "skipped_ad"
    for name in ("not-an-advertisement", "check-fails-for-good"):
        scheduled = _scheduled(by[name])
        after = scheduled[scheduled.index("classify_video") + 1 :]
        assert {"assemble_script", "synthesize_segments", "render", "publish"} <= set(after), name
    assert "EVENT_TYPE_ACTIVITY_TASK_FAILED" in by["check-fails-for-good"].read_text()


def test_the_histories_recorded_before_the_ad_check_are_really_without_it() -> None:
    older = [p for p in HISTORIES if p.parent.name not in (AD_GATE, VOICE_CHECK)]
    assert older and not any(AD_GATE in _patches(p) for p in older)
    assert not any("classify_video" in _scheduled(p) for p in older)


@pytest.mark.parametrize(
    "path",
    # A run that failed during the preparation, or ended before it, never reached the check
    # either way.
    [
        p
        for p in HISTORIES
        if p.parent.name != AD_GATE
        and p.stem not in ("voice-rate-fails-for-good", "original-published", "no-video-skipped")
    ],
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
async def test_control_the_ad_check_without_its_marker_breaks_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded before the check replay: the same check made
    without it must be rejected, or the real workflow's marker proves nothing."""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedAdGateWorkflow).replay_workflow(_history(path))


def _publish_outcome(path: Path) -> str:
    """The outcome of the one `publish` a history scheduled."""
    (publish,) = [
        e["activityTaskScheduledEventAttributes"]["input"]["payloads"][1]["data"]
        for e in json.loads(path.read_text())["events"]
        if e.get("activityTaskScheduledEventAttributes", {}).get("activityType", {}).get("name")
        == "publish"
    ]
    return json.loads(base64.b64decode(publish))["outcome"]


def test_the_voice_check_histories_carry_the_marker_and_two_end_before_the_preparation() -> None:
    """The set must really be on the new path: the marker is recorded, the check is the first
    activity, on its own queue, the video without a voice ends right after it with the
    original published, and the file without a picture right after it as skipped."""
    by = {p.stem: p for p in _of(VOICE_CHECK)}
    assert set(by) == {
        "original-published",
        "voice-localized",
        "check-fails-for-good",
        "no-video-skipped",
    }
    for path in by.values():
        assert VOICE_CHECK in _patches(path), path.stem
        on = _scheduled_on(path)
        assert on[0] == ("check_voice", "voice_check"), path.stem
        assert [name for name, _ in on].count("check_voice") == 1, path.stem
    assert _scheduled_on(by["original-published"]) == [
        ("check_voice", "voice_check"),
        ("publish", "io"),
    ]
    assert _publish_outcome(by["original-published"]) == "published_original"
    assert "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED" in by["original-published"].read_text()
    for name in ("voice-localized", "check-fails-for-good"):
        scheduled = _scheduled(by[name])
        assert scheduled[1:3] == ["ingest", "voice_rate"], name
        assert {"classify_video", "assemble_script", "render", "publish"} <= set(scheduled), name
        assert "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED" in by[name].read_text(), name
    assert "EVENT_TYPE_ACTIVITY_TASK_FAILED" in by["check-fails-for-good"].read_text()
    # No picture: skipped at once with its row, nothing prepared, nothing published.
    no_picture = by["no-video-skipped"]
    assert _scheduled_on(no_picture) == [("check_voice", "voice_check"), ("publish", "io")]
    assert _publish_outcome(no_picture) == "skipped_no_video"
    assert "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED" in no_picture.read_text()


def test_the_histories_recorded_before_the_voice_check_are_really_without_it() -> None:
    older = [p for p in HISTORIES if p.parent.name != VOICE_CHECK]
    assert older and not any(VOICE_CHECK in _patches(p) for p in older)
    assert not any("check_voice" in _scheduled(p) for p in older)
    assert all(_scheduled(p)[0] == "ingest" for p in older)


@pytest.mark.parametrize(
    "path",
    [p for p in HISTORIES if p.parent.name != VOICE_CHECK],
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
async def test_control_the_voice_check_without_its_marker_breaks_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded before the check replay: the same check made
    without it must be rejected, or the real workflow's marker proves nothing. Every older run
    began with the ingest, so every one of them must be rejected."""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedVoiceCheckWorkflow).replay_workflow(_history(path))
