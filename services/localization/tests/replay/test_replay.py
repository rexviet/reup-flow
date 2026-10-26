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
