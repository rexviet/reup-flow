"""The Vbee flow's slots as the script document the audio and caption steps read, the
lines Vbee heard as what a shortened line is checked against, and the STT step."""

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.script_document import read_script
from localization.activities.slot_script import SlotScriptActivity
from localization.activities.vbee_stt import VbeeSttActivity
from localization.contracts import SubtitleCue, SubtitleDocument, SubtitleTrack
from localization.contracts.vbee_flow import Slot, SlotPlan, SpeechTranscript, Utterance
from localization.providers.errors import PermanentProviderError, TransientProviderError
from localization.storage.object_store import ObjectStore, work_key
from tests.support.inputs import make_input

SPEC = make_input()
SUBTITLES = SubtitleTrack(subtitle_key=work_key("7001", 0, "subtitles.json"), region=None)


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        store = ObjectStore(client, "reup-media")
        doc = SubtitleDocument(
            width=1080, height=1920, region=None, cues=(SubtitleCue(start_ms=500, end_ms=1500),)
        )
        store.put_bytes(SUBTITLES.subtitle_key, doc.model_dump_json().encode())
        yield store


def slots(*texts: str) -> SlotPlan:
    return SlotPlan(
        slots=tuple(
            Slot(id=i, start_ms=i * 3000, end_ms=i * 3000 + 2500, text=t)
            for i, t in enumerate(texts)
        ),
        has_speech=bool(texts),
    )


async def write(store: ObjectStore, plan: SlotPlan, heard: SlotPlan, version: int = 1, round=0):
    act = SlotScriptActivity(store)
    return await ActivityEnvironment().run(
        act.write_slot_script, SPEC, plan, heard, SUBTITLES, version, round
    )


async def test_each_slot_is_a_segment_with_no_source_line(store: ObjectStore) -> None:
    heard = slots("xin chào các bạn", "hôm nay mua 3 cái")
    result = await write(store, heard, heard)
    assert result.script.script_key == "work/7001/r0/script.v1.json"
    assert (result.needs_human, result.flagged_segment_ids) == (False, ())
    script = read_script(store, result.script.script_key)
    assert [
        (s.id, s.start_ms, s.end_ms, s.slot_ms, s.src_zh, s.text_vi, s.on_screen)
        for s in script.segments
    ] == [
        (0, 0, 2500, 2500, "", "xin chào các bạn", True),
        (1, 3000, 5500, 2500, "", "hôm nay mua 3 cái", False),
    ]


async def test_a_later_version_goes_into_the_folder_of_its_round(store: ObjectStore) -> None:
    heard = slots("xin chào")
    result = await write(store, heard, heard, version=3, round=1)
    assert (result.script.script_key, result.script.version) == ("work/7001/r1/script.v3.json", 3)


async def test_a_video_without_speech_is_an_empty_script(store: ObjectStore) -> None:
    empty = SlotPlan(slots=(), has_speech=False)
    result = await write(store, empty, empty)
    assert read_script(store, result.script.script_key).segments == ()


async def test_a_shortened_line_that_adds_a_contact_needs_a_person(store: ObjectStore) -> None:
    heard = slots("gọi cho tôi nhé", "mua ở shop.vn nhé")
    shortened = slots("gọi 0912345678", "mua ở shop.vn")
    result = await write(store, shortened, heard)
    # The phone number is new; the domain was said in the video.
    assert (result.needs_human, result.flagged_segment_ids) == (True, (0,))


async def test_slots_that_are_not_the_heard_slots_are_refused(store: ObjectStore) -> None:
    moved = SlotPlan(
        slots=(Slot(id=0, start_ms=100, end_ms=2500, text="xin chào"),), has_speech=True
    )
    with pytest.raises(ApplicationError) as info:
        await write(store, moved, slots("xin chào"))
    assert info.value.type == "SlotMismatch" and info.value.non_retryable


# --- Vbee's STT ---------------------------------------------------------------------------


class FakeStt:
    def __init__(self, answer: SpeechTranscript | Exception) -> None:
        self.answer = answer
        self.heard: list[bytes] = []

    async def transcribe(self, wav: bytes) -> SpeechTranscript:
        self.heard.append(wav)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


HEARD = SpeechTranscript(
    utterances=(Utterance(text="xin chào", start_ms=0, end_ms=900),), audio_ms=1000
)
VOCALS = work_key("7001", 0, "vocals.wav")


async def hear(store: ObjectStore, stt: FakeStt | None):
    return await ActivityEnvironment().run(VbeeSttActivity(store, stt).vbee_stt, SPEC, VOCALS)


async def test_the_vocals_in_the_store_are_what_vbee_hears(store: ObjectStore) -> None:
    store.put_bytes(VOCALS, b"RIFF-vocals")
    stt = FakeStt(HEARD)
    assert await hear(store, stt) == HEARD
    assert stt.heard == [b"RIFF-vocals"]


@pytest.mark.parametrize(
    ("error", "retryable"),
    [(PermanentProviderError("vbee stt: HTTP 401"), False), (TransientProviderError("x"), True)],
)
async def test_a_provider_failure_leaves_as_a_temporal_error_of_its_kind(
    store: ObjectStore, error: Exception, retryable: bool
) -> None:
    store.put_bytes(VOCALS, b"RIFF-vocals")
    with pytest.raises(ApplicationError) as info:
        await hear(store, FakeStt(error))
    assert info.value.non_retryable is not retryable


async def test_lost_vocals_name_the_stage_that_made_them(store: ObjectStore) -> None:
    with pytest.raises(ApplicationError) as info:
        await hear(store, FakeStt(HEARD))
    assert info.value.type == "ArtifactMissingError" and "ingest" in str(info.value)


async def test_a_worker_without_vbee_credentials_fails_for_good(store: ObjectStore) -> None:
    store.put_bytes(VOCALS, b"RIFF-vocals")
    with pytest.raises(ApplicationError) as info:
        await hear(store, None)
    assert info.value.non_retryable and "VBEE_API_TOKEN" in str(info.value)
