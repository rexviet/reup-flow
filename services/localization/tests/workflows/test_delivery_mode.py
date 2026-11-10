"""Recorded input determines delivery even after a starter default flip/rollback."""

import asyncio
import uuid

import pytest
from temporalio import activity

from localization.config import Settings
from localization.contracts import LocalizationInput, LocalizationResult, ProjectRef, SourceSnapshot
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.replay.test_replay import _replayer
from tests.support.inputs import make_input
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.review_fakes import verdict
from tests.workflows.test_review_project_flow import ProjectReview


@pytest.mark.parametrize(
    "mode,new_default",
    [
        ("review_project", "legacy_render"),
        ("legacy_render", "review_project"),
    ],
)
async def test_running_delivery_survives_a_default_flip(
    temporal_client, monkeypatch, mode, new_default
):
    prep = PrepFakes()
    review = ProjectReview(verdicts=[verdict()])
    captured = asyncio.Event()
    release = asyncio.Event()

    @activity.defn(name="capture_review_source")
    async def capture(spec: LocalizationInput) -> SourceSnapshot:
        captured.set()
        await release.wait()
        return SourceSnapshot(
            source_key=spec.video_key,
            etag='"v1"',
            sha256="a" * 64,
            snapshot_key=f"work/7001/g{spec.generation}/r0/source.mp4",
        )

    @activity.defn(name="build_review_project")
    async def build(spec: LocalizationInput, dub, ingest, subtitles, engine) -> ProjectRef:
        return ProjectRef(project_key=f"review/7001/g{spec.generation}/head.json")

    spec = make_input().model_copy(update={"delivery_mode": mode})
    # Settings never enter the workflow. Start first, then flip the process default.
    async with prep_workers(
        temporal_client, prep, reviewing=review, project_activities=[capture, build]
    ):
        handle = await temporal_client.start_workflow(
            "VideoLocalizationWorkflow",
            spec,
            id=f"delivery-mode-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        if mode == "review_project":
            await asyncio.wait_for(captured.wait(), 20)
        monkeypatch.setenv("LOCALIZATION_DELIVERY_MODE", new_default)
        assert Settings().localization_delivery_mode == new_default
        release.set()
        answer = await asyncio.wait_for(handle.result(), 45)
    assert answer.publish.outcome == (
        "ready_for_review" if mode == "review_project" else "published"
    )
    assert (answer.publish.project_key is not None) == (mode == "review_project")
    history = await handle.fetch_history()
    names = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert ("render" in names) == (mode == "legacy_render")
    await _replayer(VideoLocalizationWorkflow).replay_workflow(history)


def test_old_input_defaults_to_legacy_even_when_starter_default_changes(monkeypatch):
    raw = make_input().model_dump(mode="json")
    raw.pop("delivery_mode")
    monkeypatch.setenv("LOCALIZATION_DELIVERY_MODE", "review_project")
    assert LocalizationInput.model_validate(raw).delivery_mode == "legacy_render"
