import asyncio
import uuid

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from localization.contracts import (
    DubbedAudio,
    IngestResult,
    LocalizationInput,
    LocalizationResult,
    ProjectRef,
    PublishRequest,
    PublishResult,
    SourceSnapshot,
    SubtitleTrack,
)
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.replay.test_replay import _replayer
from tests.support.inputs import make_input
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.review_fakes import ReviewFakes, issue, verdict


class ProjectReview(ReviewFakes):
    def activities(self):
        base = super().activities()

        @activity.defn(name="publish")
        async def publish(spec: LocalizationInput, request: PublishRequest) -> PublishResult:
            self.requests.append(request)
            return PublishResult(outcome=request.outcome, project_key=request.project_key)

        self.requests = []
        return [
            a for a in base if getattr(a, "__temporal_activity_definition").name != "publish"
        ] + [publish]


@pytest.mark.parametrize(
    "result,expected",
    [
        (verdict(), "ready_for_review"),
        (verdict(issue("dub", "overflow", 0)), "needs_human"),
        (None, "needs_human"),
        ("unavailable", "needs_human"),
    ],
)
async def test_project_delivery_never_renders_even_when_reviewer_needs_frames(
    temporal_client: Client, result, expected
):
    prep = PrepFakes()
    review = ProjectReview(
        needs_render=result is None,
        verdicts=[result] if result not in (None, "unavailable") else [verdict()],
    )
    if result == "unavailable":
        review.fail_on[("review_before_render", 1)] = ApplicationError(
            "review unavailable", non_retryable=True
        )
    seen = []

    @activity.defn(name="capture_review_source")
    async def capture(spec: LocalizationInput) -> SourceSnapshot:
        assert not prep.calls
        seen.append(spec)
        return SourceSnapshot(
            source_key=spec.video_key,
            etag='"etag"',
            snapshot_key=f"work/7001/g{spec.generation}/r0/source.mp4",
            sha256="a" * 64,
        )

    @activity.defn(name="build_review_project")
    async def build(
        spec: LocalizationInput,
        dub: DubbedAudio,
        ingest: IngestResult,
        subtitles: SubtitleTrack,
        engine: str | None,
    ) -> ProjectRef:
        assert spec.source_snapshot is not None
        return ProjectRef(project_key=f"review/7001/g{spec.generation}/head.json")

    async with prep_workers(
        temporal_client, prep, reviewing=review, project_activities=[capture, build]
    ):
        handle = await temporal_client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input().model_copy(update={"delivery_mode": "review_project"}),
            id=f"review-project-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        answer = await asyncio.wait_for(handle.result(), timeout=45)
    assert len(seen) == 1
    assert answer.publish.outcome == expected
    assert answer.publish.project_key == "review/7001/g1/head.json"
    assert answer.publish.output_key is None and answer.publish.review_key is None
    names = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in (await handle.fetch_history()).events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert names.index("capture_review_source") < names.index("ingest")
    assert names.count("localizer_source_evidence") == 1
    assert "render" not in names and "review_render" not in names
    assert review.requests[0].render_key is None
    await _replayer(VideoLocalizationWorkflow).replay_workflow(await handle.fetch_history())
