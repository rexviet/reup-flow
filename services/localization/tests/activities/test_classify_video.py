"""The ad check: one Jev call on the transcript's text, and a threshold on its one answer.

Jev is a stand-in with the real call's shape for the decisions; the recording test goes
through the real provider over a transport that sends OpenRouter's real body, so what is
sent (the text of the transcript and the one question) is what the provider would send."""

import dataclasses
import json
from pathlib import Path

import httpx2
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.classify_video import ClassifyVideoActivity
from localization.agents import ad_classifier
from localization.contracts import Transcript
from localization.observability.llm_usage import LlmUsage, RunContext
from localization.providers.jev import PERMANENT, make_client
from localization.providers.jev import Jev as RealJev
from localization.storage.object_store import ObjectStore, work_key
from tests.support.ad_fakes import AD_LINES, CHANNEL_LINES, FakeJev, transcript_document
from tests.support.file_s3 import FileS3
from tests.support.inputs import make_input

SPEC = make_input("7001", 3)
KEY = work_key("7001", 0, "transcript.json")


@pytest.fixture
def store(tmp_path: Path) -> ObjectStore:
    return ObjectStore(FileS3(tmp_path), "reup-media")


def put(store: ObjectStore, lines) -> Transcript:  # noqa: ANN001
    store.put_bytes(KEY, transcript_document(lines).model_dump_json().encode())
    return Transcript(transcript_key=KEY, segment_count=len(lines))


async def classify(
    store: ObjectStore,
    transcript: Transcript,
    jev: FakeJev,
    *,
    threshold: float = 0.8,
    enabled: bool = True,
) -> bool:
    act = ClassifyVideoActivity(
        store,
        lambda: jev,  # type: ignore[arg-type,return-value]
        threshold=threshold,
        enabled=enabled,
    )
    return await ActivityEnvironment().run(act.classify_video, SPEC, transcript)


async def test_an_advertisement_is_skipped_on_the_text_of_its_transcript(
    store: ObjectStore,
) -> None:
    jev = FakeJev(score=0.99)
    assert await classify(store, put(store, AD_LINES), jev) is True
    ((state, questions),) = jev.asked
    # The whole transcript, as text, and one question: nothing else leaves the worker.
    assert state == {"transcript": [{"id": i, "text": t} for i, t in enumerate(AD_LINES)]}
    assert questions == ["promotes_product"]


async def test_speech_that_sells_nothing_is_localized(store: ObjectStore) -> None:
    jev = FakeJev(score=0.01)
    assert await classify(store, put(store, CHANNEL_LINES), jev) is False
    assert len(jev.asked) == 1


@pytest.mark.parametrize(
    ("score", "threshold", "skip"),
    [(0.8, 0.8, True), (0.79, 0.8, False), (0.85, 0.9, False), (0.85, 0.5, True)],
)
async def test_the_threshold_of_the_settings_decides(
    store: ObjectStore, score: float, threshold: float, skip: bool
) -> None:
    jev = FakeJev(score=score)
    assert await classify(store, put(store, AD_LINES), jev, threshold=threshold) is skip


async def test_switched_off_nothing_is_read_or_asked(store: ObjectStore) -> None:
    """The store holds no transcript: reading it would fail the activity."""
    jev = FakeJev(score=0.99)
    transcript = Transcript(transcript_key=KEY, segment_count=3)
    assert await classify(store, transcript, jev, enabled=False) is False
    assert jev.asked == []


async def test_a_transcript_without_segments_is_not_classified(store: ObjectStore) -> None:
    jev = FakeJev(score=0.99)
    assert await classify(store, Transcript(transcript_key=KEY, segment_count=0), jev) is False
    assert jev.asked == []


@pytest.mark.parametrize("lines", [(), ("", "  ", "\n")], ids=["no-segments", "blank-segments"])
async def test_a_transcript_with_no_words_is_not_classified(
    store: ObjectStore, lines: tuple[str, ...]
) -> None:
    jev = FakeJev(score=0.99)
    transcript = put(store, lines)
    # The count the transcription reported says there is speech; the document says none.
    transcript = transcript.model_copy(update={"segment_count": 3})
    assert await classify(store, transcript, jev) is False
    assert jev.asked == []


async def test_blank_lines_are_left_out_of_what_jev_reads(store: ObjectStore) -> None:
    jev = FakeJev(score=0.01)
    await classify(store, put(store, ("", "转发", "  ")), jev)
    ((state, _),) = jev.asked
    assert state == {"transcript": [{"id": 1, "text": "转发"}]}


async def test_a_provider_error_is_raised_as_the_provider_classified_it(
    store: ObjectStore,
) -> None:
    """The workflow lets the video through on it; the activity does not hide it."""
    error = ApplicationError("jev: HTTP 401", type=PERMANENT, non_retryable=True)
    with pytest.raises(ApplicationError) as raised:
        await classify(store, put(store, AD_LINES), FakeJev(fail=error))
    assert raised.value.type == PERMANENT and raised.value.non_retryable


async def test_a_lost_transcript_is_a_lost_artifact(store: ObjectStore) -> None:
    with pytest.raises(ApplicationError) as raised:
        await classify(store, Transcript(transcript_key=KEY, segment_count=2), FakeJev())
    assert raised.value.type == "ArtifactMissingError"


def test_the_decision_is_one_question_against_the_threshold() -> None:
    assert list(ad_classifier.QUESTIONS) == [ad_classifier.QUESTION] == ["promotes_product"]
    assert ad_classifier.is_advertisement({"promotes_product": 0.8}, 0.8)
    assert not ad_classifier.is_advertisement({"promotes_product": 0.05}, 0.8)


async def test_the_call_is_recorded_under_the_activitys_stage_with_the_cost_openrouter_reported(
    store: ObjectStore,
) -> None:
    recorded: list[tuple[LlmUsage, RunContext]] = []
    sent: list[dict] = []

    async def recorder(usage: LlmUsage, run: RunContext) -> None:
        recorded.append((usage, run))

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx2.Response(
            200,
            json={
                "model": "typesafe/jev-1.13-20260917",
                "answers": {n: {"type": "noul", "noul": 0.99} for n in body["questions"]},
                "usage": {"input_tokens": 380, "output_tokens": 12, "cost": 0.0000112},
                "id": "gen-ad-1793112141-abc",
                "provider": "TypeSafe",
            },
        )

    client = make_client(
        "k",
        base_url="https://openrouter.ai/api",
        model="typesafe/jev-1.13",
        transport=httpx2.MockTransport(handler),
    )
    real = RealJev(client, model="typesafe/jev-1.13")
    act = ClassifyVideoActivity(store, lambda: real, threshold=0.8, recorder=recorder)
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info, workflow_id="video-7001-g3", activity_type="classify_video"
    )
    assert await env.run(act.classify_video, SPEC, put(store, AD_LINES)) is True
    (body,) = sent
    assert set(body["questions"]) == {"promotes_product"}
    assert body["state"] == {"transcript": [{"id": i, "text": t} for i, t in enumerate(AD_LINES)]}
    ((usage, run),) = recorded
    assert (run.aweme_id, run.generation, run.stage) == ("7001", 3, "classify_video")
    assert usage.generation_id == "gen-ad-1793112141-abc"
    assert usage.cost_usd == pytest.approx(0.0000112)
