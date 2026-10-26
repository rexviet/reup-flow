"""Replays recorded histories against the current workflow code.

A run that is already in flight when new workflow code is deployed resumes by
replaying its history. If the code now issues its commands in a different order,
the replay fails and the run is stuck. Every history recorded under
`histories/` must keep replaying; a change that reorders commands must be guarded
with `workflow.patched("<name>")` so old histories still take their old path.
"""

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


def test_there_are_histories_to_replay() -> None:
    """An empty directory would make every replay test below pass vacuously."""
    names = {p.stem for p in HISTORIES}
    assert {"plain-run", "no-burned-in-subtitles", "lost-vocals-ingest-rerun"} <= names


@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_every_recorded_history_still_replays(path: Path) -> None:
    await _replayer(VideoLocalizationWorkflow).replay_workflow(_history(path))


@pytest.mark.parametrize("path", HISTORIES, ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_control_a_reordered_workflow_fails_to_replay(path: Path) -> None:
    """The replay is only a guard if it can fail: the same histories against a
    workflow that starts 2b before 2a must be rejected."""
    with pytest.raises(NondeterminismError):
        await _replayer(ReorderedWorkflow).replay_workflow(_history(path))
