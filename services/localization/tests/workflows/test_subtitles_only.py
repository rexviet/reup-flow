"""A subtitles-only run: the burned-in subtitles read, translated and laid over the source's
own picture and sound. Nothing is separated, heard, classified or spoken. The workflow runs
for real; the activities are the test stand-ins, the Localizer a scripted model."""

import asyncio
import uuid
from collections.abc import Callable

from temporalio import activity
from temporalio.client import Client, WorkflowHandle

from localization.contracts import (
    DubbedAudio,
    IngestResult,
    LocalizationInput,
    LocalizationResult,
    LocalizedScriptRef,
    ProjectRef,
    PublishRequest,
    PublishResult,
    SourceSnapshot,
    SubtitleTrack,
    Transcript,
)
from localization.storage.object_store import work_key
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.replay.test_replay import _replayer
from tests.support.inputs import make_input
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.review_fakes import ReviewFakes

# What the dubbed flow runs and this one must not.
NOT_RUN = {
    "check_voice",
    "ingest",
    "transcribe",
    "voice_rate",
    "classify_video",
    "detect_singing",
    "voice_profile",
    "measure_fit",
    "commit_dub",
    "synthesize_segments",
    "assemble_audio",
    "review_before_render",
    "review_render",
    "render",
}


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


class SubtitlePrep(PrepFakes):
    """The preparation of a run that hears nothing: `read` lines are read from the
    subtitles on screen."""

    def __init__(self, read: int = 2, **kwargs) -> None:
        super().__init__(**kwargs)
        self.read = read

    def activities(self) -> list[Callable]:
        base = [
            a
            for a in super().activities()
            if getattr(a, "__temporal_activity_definition").name != "complete_transcript"
        ]
        fakes = self

        @activity.defn(name="complete_transcript")
        async def complete_transcript(
            spec: LocalizationInput, transcript: Transcript, subtitles: SubtitleTrack
        ) -> Transcript:
            fakes.calls["complete_transcript"] += 1
            assert transcript.segment_count == 0
            return Transcript(
                transcript_key=work_key(spec.aweme_id, 0, "transcript-subtitles.json"),
                segment_count=fakes.read,
            )

        return [*base, complete_transcript]


def _subtitle_activities(seen: dict) -> tuple[list, list]:
    @activity.defn(name="capture_review_source")
    async def capture(spec: LocalizationInput) -> SourceSnapshot:
        return SourceSnapshot(
            source_key=spec.video_key,
            etag='"etag"',
            snapshot_key=f"work/7001/g{spec.generation}/r0/source.mp4",
            sha256="a" * 64,
        )

    @activity.defn(name="source_audio")
    async def source_audio(spec: LocalizationInput) -> IngestResult:
        assert spec.source_snapshot is not None
        key = work_key(spec.aweme_id, 0, "source-audio.wav")
        return IngestResult(
            vocals_key=key, bgm_key=key, duration_ms=3000, width=1080, height=1920, fps=30
        )

    @activity.defn(name="empty_transcript")
    async def empty_transcript(spec: LocalizationInput) -> Transcript:
        return Transcript(
            transcript_key=work_key(spec.aweme_id, 0, "transcript-none.json"), segment_count=0
        )

    @activity.defn(name="keep_original_dub")
    async def keep_original_dub(
        spec: LocalizationInput, script: LocalizedScriptRef, ingest: IngestResult
    ) -> DubbedAudio:
        return DubbedAudio(
            audio_key=ingest.bgm_key,
            audio_sha256="b" * 64,
            script_key=script.script_key,
            script_version=script.version,
            segments_key=work_key(spec.aweme_id, 0, "dub-segments.json"),
        )

    @activity.defn(name="build_review_project")
    async def build(
        spec: LocalizationInput,
        dub: DubbedAudio,
        ingest: IngestResult,
        subtitles: SubtitleTrack,
        engine: str | None,
    ) -> ProjectRef:
        seen["project"] = (dub, ingest)
        return ProjectRef(project_key=f"review/7001/g{spec.generation}/head.json")

    return [capture, empty_transcript, keep_original_dub, build], [source_audio]


def _spec() -> LocalizationInput:
    return make_input().model_copy(
        update={"delivery_mode": "review_project", "localize_mode": "subtitles_only"}
    )


async def _run(client: Client, prep: PrepFakes) -> tuple[LocalizationResult, list, dict, list]:
    seen: dict = {}
    review = ProjectReview()
    io, media = _subtitle_activities(seen)
    async with prep_workers(
        client, prep, reviewing=review, project_activities=io, media_activities=media
    ):
        handle: WorkflowHandle = await client.start_workflow(
            "VideoLocalizationWorkflow",
            _spec(),
            id=f"subtitles-only-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        answer = await asyncio.wait_for(handle.result(), timeout=45)
    history = await handle.fetch_history()
    names = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    await _replayer(VideoLocalizationWorkflow).replay_workflow(history)
    return answer, names, seen, review.requests


async def test_the_subtitles_are_translated_over_the_source_sound(temporal_client: Client):
    answer, names, seen, requests = await _run(temporal_client, SubtitlePrep())
    assert not NOT_RUN & set(names), NOT_RUN & set(names)
    assert names.index("capture_review_source") < names.index("source_audio")
    for step in ("detect_subtitles", "complete_transcript", "assemble_script"):
        assert step in names
    assert names.index("keep_original_dub") < names.index("layout_captions")
    assert names.index("layout_captions") < names.index("build_review_project")
    dub, ingest = seen["project"]
    assert dub.audio_key == ingest.bgm_key
    (request,) = requests
    assert request.outcome == "ready_for_review"
    assert request.project_key == "review/7001/g1/head.json"
    assert request.verdict is None and request.reasons == ()
    assert answer.publish.outcome == "ready_for_review"


async def test_a_video_without_burned_in_subtitles_goes_to_a_person(temporal_client: Client):
    answer, names, seen, requests = await _run(temporal_client, SubtitlePrep(region=None))
    assert "complete_transcript" not in names and "build_review_project" not in names
    (request,) = requests
    assert request.outcome == "needs_human"
    assert request.project_key is None and request.render_key is None
    assert "no burned-in subtitles" in request.reasons[0]
    assert answer.script is None


async def test_subtitles_nobody_could_read_go_to_a_person(temporal_client: Client):
    answer, names, seen, requests = await _run(temporal_client, SubtitlePrep(read=0))
    assert "assemble_script" not in names and "build_review_project" not in names
    (request,) = requests
    assert request.outcome == "needs_human"
    assert "no subtitle line could be read" in request.reasons[0]
