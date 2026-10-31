"""The voice check inside the real workflow: a video without a voice ends before anything is
prepared, its original published as it is and its run left as `published_original`; a file
without a video stream ends there too, skipped as `skipped_no_video` whatever its audio holds;
every other video (one with a voice, one at the threshold, one the check could not measure,
any while the check is off) goes on exactly as before.

The check is the real `check_voice` activity (download, probe, decode) on a stand-in
detector; publishing is the real activity over the same bucket and a run table in memory;
the Localizer and the Dubber are the real agents on scripted models."""

import asyncio
import uuid

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from localization.contracts import LocalizationResult
from localization.providers.errors import PermanentProviderError, TransientProviderError
from localization.storage.object_store import output_key, review_key
from tests.support.dub_fakes import DubFakes
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.review_fakes import ReviewFakes
from tests.support.voice_fakes import FakeDetector, VoiceFakes, source_video

PREPARATION = {"ingest", "voice_rate", "transcribe", "detect_subtitles"}
# What a video that is localized runs after the preparation (any one of them is proof enough).
LOCALIZED = {
    "classify_video",
    "assemble_script",
    "voice_profile",
    "synthesize_segments",
    "commit_dub",
    "assemble_audio",
    "layout_captions",
    "review_before_render",
    "render",
}


class Run:
    def __init__(self, voicing: VoiceFakes, preparing: PrepFakes) -> None:
        self.voicing = voicing
        self.preparing = preparing
        self.reviewing = ReviewFakes()
        self.handle = None
        self.outcome: LocalizationResult | BaseException | None = None

    @property
    def result(self) -> LocalizationResult:
        assert isinstance(self.outcome, LocalizationResult), self.outcome
        return self.outcome

    def row(self) -> dict:
        return self.reviewing.runs.rows[("7001", 1)]

    async def scheduled(self) -> list[tuple[str, str]]:
        """Each activity the run scheduled, with the queue it was sent to."""
        history = await self.handle.fetch_history()
        return [
            (
                e.activity_task_scheduled_event_attributes.activity_type.name,
                e.activity_task_scheduled_event_attributes.task_queue.name,
            )
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        ]


async def run_workflow(
    client: Client, voicing: VoiceFakes, preparing: PrepFakes | None = None, timeout: float = 60
) -> Run:
    run = Run(voicing, preparing or PrepFakes())
    async with prep_workers(
        client,
        run.preparing,
        LocalizerFakes(),
        dubbing=DubFakes(),
        reviewing=run.reviewing,
        voicing=voicing,
    ):
        run.handle = await client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        try:
            run.outcome = await asyncio.wait_for(run.handle.result(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            run.outcome = exc
    return run


def names(scheduled: list[tuple[str, str]]) -> list[str]:
    return [name for name, _ in scheduled]


def localized_as_usual(run: Run, scheduled: list[tuple[str, str]]) -> None:
    """The run went past the check to the end it has always had."""
    assert run.result.publish is not None and run.result.publish.outcome == "published"
    assert run.result.prep is not None
    assert run.result.script is not None and run.result.dub is not None
    order = names(scheduled)
    assert LOCALIZED <= set(order), order
    # Asked once, first, on its own queue; the preparation follows it.
    assert order.count("check_voice") == 1
    assert scheduled[0] == ("check_voice", "voice_check")
    assert set(order[1:5]) == PREPARATION, order
    assert run.preparing.calls["ingest"] == 1
    row = run.row()
    assert (row["status"], row["review"]) == ("published", None)
    # What went out is the localized render, not the source.
    assert run.reviewing.store.get_bytes(output_key("7001"), produced_by="t") != source_video(
        run.voicing.source
    )


async def test_a_video_without_a_voice_publishes_its_original_before_anything_is_prepared(
    temporal_client: Client,
) -> None:
    run = await run_workflow(temporal_client, VoiceFakes(detector=FakeDetector(share=0.0)))
    scheduled = await run.scheduled()
    # The check and the row: no ingest, no rate, no speech-to-text, no subtitles, no agent.
    assert scheduled == [("check_voice", "voice_check"), ("publish", "io")]
    assert sum(run.preparing.calls.values()) == 0
    result = run.result
    assert result.prep is None and result.script is None and result.dub is None
    assert result.publish is not None
    assert (result.publish.outcome, result.publish.output_key) == (
        "published_original",
        output_key("7001"),
    )
    assert result.publish.review_key is None
    row = run.row()
    assert (row["status"], row["needs_human"], row["review_rounds"]) == (
        "published_original",
        False,
        0,
    )
    assert (row["output"], row["review"]) == (output_key("7001"), None)
    assert row["record"] == {"reasons": [], "verdict": None}
    assert run.reviewing.runs.writes == 1
    # The original, byte for byte, is what went out; nothing was kept for a person.
    store = run.reviewing.store
    assert store.get_bytes(output_key("7001"), produced_by="t") == source_video("picture_and_sound")
    assert not store.exists(review_key("7001", 1))
    assert run.reviewing.renders == [] and run.reviewing.reviews == []
    # The real decode reached the detector: two seconds of audio (and the encoder's padding).
    (seconds,) = run.voicing.detector.measured
    assert 1.9 < seconds < 2.1


async def test_a_video_with_a_voice_is_localized_as_before(temporal_client: Client) -> None:
    run = await run_workflow(temporal_client, VoiceFakes(detector=FakeDetector(share=0.4)))
    localized_as_usual(run, await run.scheduled())


async def test_a_video_at_the_threshold_has_a_voice(temporal_client: Client) -> None:
    run = await run_workflow(
        temporal_client, VoiceFakes(detector=FakeDetector(share=0.05), min_speech_share=0.05)
    )
    localized_as_usual(run, await run.scheduled())


async def test_a_file_without_a_picture_is_skipped_before_anything_is_prepared(
    temporal_client: Client,
) -> None:
    """Sound only: pictures with music laid over them. Skipped at once, whatever the audio
    holds (the detector here would call it all speech), nothing published."""
    voicing = VoiceFakes(source="sound_only", detector=FakeDetector(share=0.9))
    # The ingest would refuse it; it must never be reached.
    refused = ApplicationError("no_video_stream", type="PermanentProviderError", non_retryable=True)
    run = await run_workflow(temporal_client, voicing, PrepFakes(ingest_error=refused))
    assert await run.scheduled() == [("check_voice", "voice_check"), ("publish", "io")]
    assert sum(run.preparing.calls.values()) == 0
    result = run.result
    assert result.prep is None and result.script is None and result.dub is None
    assert result.publish is not None
    assert (result.publish.outcome, result.publish.output_key, result.publish.review_key) == (
        "skipped_no_video",
        None,
        None,
    )
    row = run.row()
    assert (row["status"], row["needs_human"], row["review_rounds"]) == (
        "skipped_no_video",
        False,
        0,
    )
    assert (row["output"], row["review"]) == (None, None)
    assert run.reviewing.runs.writes == 1
    assert not run.reviewing.store.exists(output_key("7001"))
    assert not run.reviewing.store.exists(review_key("7001", 1))
    assert run.reviewing.renders == [] and run.reviewing.reviews == []
    # Not heard: the check stopped at the probe.
    assert voicing.detector.measured == []


async def test_a_check_that_fails_for_good_lets_the_video_through(
    temporal_client: Client,
) -> None:
    voicing = VoiceFakes(detector=FakeDetector(share=0.0, fail=PermanentProviderError("vad")))
    run = await run_workflow(temporal_client, voicing)
    localized_as_usual(run, await run.scheduled())
    # A permanent error is not retried; the run neither fails nor waits on it.
    assert voicing.calls == 1


async def test_a_check_whose_retries_are_spent_lets_the_video_through(
    temporal_client: Client,
) -> None:
    voicing = VoiceFakes(detector=FakeDetector(share=0.0, fail=TransientProviderError("vad")))
    run = await run_workflow(temporal_client, voicing, timeout=90)
    localized_as_usual(run, await run.scheduled())
    # Every attempt the retry policy allows was made, then the video went on.
    assert voicing.calls == 3


async def test_with_the_check_switched_off_every_video_is_localized(
    temporal_client: Client,
) -> None:
    voicing = VoiceFakes(detector=FakeDetector(share=0.0), enabled=False)
    run = await run_workflow(temporal_client, voicing)
    localized_as_usual(run, await run.scheduled())
    assert voicing.detector.measured == []
