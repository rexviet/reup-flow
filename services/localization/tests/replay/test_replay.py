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
    for phase in ("phase-04", "phase-05"):
        names = {p.stem for p in _of(phase)}
        assert {"plain-run", "no-burned-in-subtitles", "lost-vocals-ingest-rerun"} <= names, phase
    assert {"glossary-and-estimate-tools", "answer-rejected-then-fixed"} <= {
        p.stem for p in _of("phase-05")
    }


def test_the_histories_recorded_before_the_localizer_stage_are_really_without_it() -> None:
    """They are the proof that a run already in flight when the stage shipped still
    replays; a recording that already had the stage would prove nothing."""
    for path in _of("phase-04"):
        text = path.read_text()
        assert "voice_rate" not in text, path.name
        assert "localize-script" not in _patches(path), path.name


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
