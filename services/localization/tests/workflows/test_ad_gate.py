"""The ad check inside the real workflow: a prepared video whose speech sells a product
ends there, as `skipped_ad`, and nothing after the preparation runs; every other video
(one that sells nothing, one without speech, any while the check is off, one Jev could
not answer for) goes on exactly as before.

The check is the real `classify_video` activity on a stand-in Jev; publishing is the real
activity over a run table in memory; the Localizer and the Dubber are the real agents on
scripted models."""

import asyncio
import uuid

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from localization.contracts import LocalizationResult
from localization.storage.object_store import output_key, review_key
from tests.support.ad_fakes import AD_LINES, AdFakes, FakeJev
from tests.support.dub_fakes import DubFakes
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.review_fakes import ReviewFakes

# The voice check every run starts with (the video has a voice), then the preparation.
PREPARATION = {"check_voice", "ingest", "voice_rate", "transcribe", "detect_subtitles"}
# What a video that is localized runs after the check (any one of them is proof enough).
AFTER_THE_GATE = {
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
    def __init__(self, classifying: AdFakes) -> None:
        self.classifying = classifying
        self.reviewing = ReviewFakes()
        self.handle = None
        self.outcome: LocalizationResult | BaseException | None = None

    @property
    def result(self) -> LocalizationResult:
        assert isinstance(self.outcome, LocalizationResult), self.outcome
        return self.outcome

    def row(self) -> dict:
        return self.reviewing.runs.rows[("7001", 1)]

    async def scheduled(self) -> list[str]:
        history = await self.handle.fetch_history()
        return [
            e.activity_task_scheduled_event_attributes.activity_type.name
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        ]


async def run_workflow(client: Client, classifying: AdFakes) -> Run:
    run = Run(classifying)
    async with prep_workers(
        client,
        PrepFakes(),
        LocalizerFakes(),
        dubbing=DubFakes(),
        reviewing=run.reviewing,
        classifying=classifying,
    ):
        run.handle = await client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        try:
            run.outcome = await asyncio.wait_for(run.handle.result(), timeout=60)
        except Exception as exc:  # noqa: BLE001
            run.outcome = exc
    return run


def localized_as_usual(run: Run, names: list[str]) -> None:
    """The run went past the check to the end it has always had."""
    assert run.result.publish is not None and run.result.publish.outcome == "published"
    assert run.result.script is not None and run.result.dub is not None
    assert AFTER_THE_GATE <= set(names), names
    assert names.count("classify_video") == 1
    # Asked once the video is prepared, before anything of the Localizer stage.
    assert set(names[: names.index("classify_video")]) == PREPARATION
    assert run.row()["status"] == "published"


async def test_an_advertisement_ends_after_the_preparation_as_skipped_ad(
    temporal_client: Client,
) -> None:
    run = await run_workflow(temporal_client, AdFakes(lines=AD_LINES, jev=FakeJev(score=0.99)))
    names = await run.scheduled()
    # The preparation, the check, the row: no Localizer, no Dubber, no synthesis, no
    # layout, render or review.
    assert set(names) == PREPARATION | {"classify_video", "publish"}, names
    assert names[-2:] == ["classify_video", "publish"]
    assert not any(n.startswith("agent__") for n in names)
    assert run.result.publish is not None
    assert (run.result.publish.outcome, run.result.publish.output_key) == ("skipped_ad", None)
    assert run.result.publish.review_key is None
    assert run.result.script is None and run.result.dub is None
    row = run.row()
    assert (row["status"], row["needs_human"], row["review_rounds"]) == ("skipped_ad", False, 0)
    assert (row["output"], row["review"]) == (None, None)
    assert run.reviewing.runs.writes == 1
    assert not run.reviewing.store.exists(output_key("7001"))
    assert not run.reviewing.store.exists(review_key("7001", 1))
    assert run.reviewing.renders == [] and run.reviewing.reviews == []
    ((state, questions),) = run.classifying.jev.asked
    assert [line["text"] for line in state["transcript"]] == list(AD_LINES)
    assert questions == ["promotes_product"]


async def test_a_video_that_sells_nothing_is_localized_as_before(temporal_client: Client) -> None:
    run = await run_workflow(temporal_client, AdFakes(jev=FakeJev(score=0.02)))
    localized_as_usual(run, await run.scheduled())
    assert len(run.classifying.jev.asked) == 1


async def test_a_video_without_speech_is_localized_and_jev_is_not_asked(
    temporal_client: Client,
) -> None:
    run = await run_workflow(temporal_client, AdFakes(lines=(), jev=FakeJev(score=0.99)))
    localized_as_usual(run, await run.scheduled())
    assert run.classifying.jev.asked == []


async def test_with_the_check_switched_off_every_video_is_localized(
    temporal_client: Client,
) -> None:
    run = await run_workflow(
        temporal_client, AdFakes(lines=AD_LINES, jev=FakeJev(score=0.99), enabled=False)
    )
    localized_as_usual(run, await run.scheduled())
    assert run.classifying.jev.asked == []


async def test_a_check_that_fails_for_good_lets_the_video_through(
    temporal_client: Client,
) -> None:
    failing = FakeJev(
        fail=ApplicationError("jev: HTTP 401", type="PermanentProviderError", non_retryable=True)
    )
    run = await run_workflow(temporal_client, AdFakes(lines=AD_LINES, jev=failing))
    localized_as_usual(run, await run.scheduled())
    # A permanent error is not retried; the run neither fails nor waits on it.
    assert run.classifying.calls == 1 and len(failing.asked) == 1
