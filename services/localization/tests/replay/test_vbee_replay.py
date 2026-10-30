"""Replays the recorded histories of the Vbee dubbing workflow against its current code.

A run in flight when new code is deployed resumes by replaying its history; a change that
reorders the commands breaks it. The set is recorded with
`uv run python -m tests.replay.record_histories vbee-dubbing` and kept apart from the
other workflow's sets, which `test_replay.py` replays against that workflow."""

import json
from pathlib import Path

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError

from localization.temporal.client import guarded_data_converter
from localization.workflows.vbee_dubbing import VbeeDubbingWorkflow
from tests.replay.test_replay import HISTORIES as OTHER_HISTORIES
from tests.replay.vbee_controls import DetectFirstVbeeWorkflow, UncorrectedVbeeWorkflow

HISTORIES = sorted((Path(__file__).parent / "vbee_histories").glob("*.json"))


def _replayer(workflow: type) -> Replayer:
    return Replayer(
        workflows=[workflow],
        data_converter=guarded_data_converter(),
        plugins=[PydanticAIPlugin()],
    )


def _history(path: Path) -> WorkflowHistory:
    return WorkflowHistory.from_json(f"replay-{path.stem}", path.read_text())


def _activities(path: Path) -> list[str]:
    return [
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in json.loads(path.read_text())["events"]
        if "activityTaskScheduledEventAttributes" in e
    ]


def _named(stem: str) -> Path:
    (path,) = [p for p in HISTORIES if p.stem == stem]
    return path


def test_there_are_histories_to_replay_and_they_are_the_vbee_workflows() -> None:
    """An empty directory would make every replay below pass vacuously."""
    assert {p.stem for p in HISTORIES} >= {
        "published-at-the-first-review",
        "lines-shortened-at-the-measured-rate",
        "review-shortens-one-line",
        "needs-a-person-after-two-reviews",
        "line-that-cannot-be-shortened",
        "no-speech",
        "stt-fails-for-good",
    }
    for path in HISTORIES:
        started = json.loads(path.read_text())["events"][0]
        attributes = started["workflowExecutionStartedEventAttributes"]
        assert attributes["workflowType"]["name"] == "VbeeDubbingWorkflow", path
    # And none of them is among the histories replayed against the other workflow.
    assert not set(HISTORIES) & set(OTHER_HISTORIES)


def test_the_histories_take_the_paths_they_are_named_for() -> None:
    measured = _activities(_named("lines-shortened-at-the-measured-rate"))
    at = measured.index("clip_durations")
    assert measured[at + 1] == "agent__slot_shortener__model_request"
    assert _activities(_named("review-shortens-one-line")).count("review_dub") == 2
    assert _activities(_named("needs-a-person-after-two-reviews")).count("review_dub") == 2
    assert "synthesize_segments" not in _activities(_named("no-speech"))
    failed = _activities(_named("stt-fails-for-good"))
    assert "plan_slots" not in failed and failed[-1] == "publish"


@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: p.stem)
async def test_every_recorded_history_still_replays(path: Path) -> None:
    await _replayer(VbeeDubbingWorkflow).replay_workflow(_history(path))


@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: p.stem)
async def test_control_a_workflow_that_starts_in_another_order_fails_to_replay(path: Path) -> None:
    with pytest.raises(NondeterminismError):
        await _replayer(DetectFirstVbeeWorkflow).replay_workflow(_history(path))


@pytest.mark.parametrize(
    "path",
    [p for p in HISTORIES if "clip_durations" in _activities(p)],
    ids=lambda p: p.stem,
)
async def test_control_a_workflow_without_the_rate_correction_fails_to_replay(path: Path) -> None:
    with pytest.raises(NondeterminismError):
        await _replayer(UncorrectedVbeeWorkflow).replay_workflow(_history(path))
