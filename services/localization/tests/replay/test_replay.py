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
from tests.support.reordered_flow import ReorderedWorkflow
from tests.support.ungated_dub_flow import UngatedDubWorkflow
from tests.support.ungated_rate_flow import UngatedRateWorkflow
from tests.support.ungated_review_flow import UngatedReviewWorkflow
from tests.support.ungated_route_flow import UngatedRouteWorkflow

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
        if path.parent.name == NEW_RATE:
            continue
        assert "voice-rate-with-ingest" not in _patches(path), path.name
        scheduled = _scheduled(path)
        if "voice_rate" in scheduled:
            assert scheduled.index("voice_rate") > scheduled.index("detect_subtitles"), path.name


@pytest.mark.parametrize(
    "path",
    [p for p in HISTORIES if p.parent.name != NEW_RATE],
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
async def test_control_the_rate_beside_the_ingest_without_its_marker_breaks_the_older_histories(
    path: Path,
) -> None:
    """The marker is what lets a run recorded before the rate moved replay: the same move
    made without it must be rejected, or the real workflow's marker proves nothing."""
    with pytest.raises(NondeterminismError):
        await _replayer(UngatedRateWorkflow).replay_workflow(_history(path))
