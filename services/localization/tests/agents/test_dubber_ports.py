"""What the Dubber's tools reach on a worker. The tool that reads a line runs on the
`io` queue, which has no TTS limit, so it must never speak; only `speak` may, and it
shares the cache with the batch synthesis."""

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError

from localization.activities.dubber_ports import StoreDubberPorts
from localization.activities.synthesize import META_DURATION, META_TEXT, META_VOICE, Synthesizer
from localization.agents.dubber import DubberDeps
from localization.storage.object_store import ObjectStore
from tests.support.inputs import make_input
from tests.support.tts_fakes import FakeTts, put_script, segment

VOICE = make_input().voice
ENGINE = "eng"


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        yield ObjectStore(client, "reup-media")


def deps(script_key: str) -> DubberDeps:
    return DubberDeps(
        aweme_id="7001",
        script_key=script_key,
        voice=VOICE,
        engine=ENGINE,
        syllables_per_sec=4.0,
        pending_ids=(0,),
    )


async def test_reading_a_line_reports_its_slot_and_how_long_its_clip_is(
    store: ObjectStore,
) -> None:
    tts = FakeTts(seconds_per_char=0.1)
    synth = Synthesizer(store, tts, model="tts-1")
    key = put_script(store, [segment(0, "x" * 20, slot_ms=1500)])
    await synth.clip(VOICE, ENGINE, "x" * 20)
    seen = await StoreDubberPorts(store, synth).segment(deps(key), 0)
    assert (seen.id, seen.slot_ms, seen.text_vi, seen.src_zh) == (0, 1500, "x" * 20, "你好")
    assert seen.duration_ms == pytest.approx(2000, abs=30)


async def test_reading_a_line_never_speaks_even_when_its_clip_is_missing(
    store: ObjectStore,
) -> None:
    tts = FakeTts()
    synth = Synthesizer(store, tts, model="tts-1")
    key = put_script(store, [segment(0, "chưa có clip")])
    with pytest.raises(ApplicationError) as failure:
        await StoreDubberPorts(store, synth).segment(deps(key), 0)
    assert failure.value.type == "ClipMissing"
    assert failure.value.non_retryable
    assert tts.total == 0


async def test_speaking_a_line_makes_a_clip_the_batch_synthesis_then_finds(
    store: ObjectStore,
) -> None:
    tts = FakeTts()
    synth = Synthesizer(store, tts, model="tts-1")
    key = put_script(store, [segment(0, "câu gốc")])
    spoken = await StoreDubberPorts(store, synth).speak(deps(key), "câu mới")
    assert spoken.duration_ms > 0
    again = await Synthesizer(store, tts, model="tts-1").clip(VOICE, ENGINE, "câu mới")
    assert again.duration_ms == spoken.duration_ms
    assert tts.calls["câu mới"] == 1


def test_the_clip_metadata_names_are_the_ones_other_stages_read() -> None:
    """Caption checks and the review read these by name, so a rename is a contract
    change. Hyphens, because S3 folds an underscore in a metadata name into one."""
    assert (META_TEXT, META_VOICE, META_DURATION) == ("text-vi", "voice-hash", "duration-ms")
