"""The Vbee voice a run names by a marker, and the provider each voice is spoken by.

The configured voice id is a canary here: it must reach the provider (or the run would be
spoken in another voice) and change every cache key built from the voice, and it must not
appear in anything a run keeps: the plan's hashes, an error, the clip's metadata."""

import boto3
import pytest
from moto import mock_aws
from pydantic import SecretStr
from temporalio.testing import ActivityEnvironment
from tests.support.inputs import make_input
from tests.support.tts_fakes import FakeTts, put_script, segment

from localization.activities.synthesize import SynthesizeActivities
from localization.activities.voice_rate import SAMPLE_TEXT, VoiceRateActivity
from localization.contracts import LocalizationInput
from localization.contracts.vbee_flow import VBEE_CONFIGURED_VOICE
from localization.contracts.voice import VbeeSettings, VoiceRef
from localization.providers.errors import PermanentProviderError
from localization.providers.tts_router import ConfiguredVoices, TtsByProvider
from localization.storage.object_store import ObjectStore

CANARY = "vbee-voice-canary-5d1f"
MARKED = VoiceRef(provider="vbee", voice_id=VBEE_CONFIGURED_VOICE, settings=VbeeSettings())
STUDIO = make_input().voice


def vbee_input() -> LocalizationInput:
    return make_input().model_copy(update={"voice": MARKED})


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        yield ObjectStore(client, "reup-media")


def test_the_marker_becomes_the_configured_voice_and_nothing_else_changes() -> None:
    resolved = ConfiguredVoices(SecretStr(CANARY)).resolve(MARKED)
    assert resolved.voice_id == CANARY
    assert (resolved.provider, resolved.settings) == (MARKED.provider, MARKED.settings)


def test_a_voice_of_another_provider_is_left_as_it_is() -> None:
    assert ConfiguredVoices(SecretStr(CANARY)).resolve(STUDIO) is STUDIO


def test_a_vbee_voice_that_names_its_own_id_is_refused_without_echoing_it() -> None:
    own = MARKED.model_copy(update={"voice_id": "someone-elses-voice"})
    with pytest.raises(PermanentProviderError) as info:
        ConfiguredVoices(SecretStr(CANARY)).resolve(own)
    assert "someone-elses-voice" not in str(info.value)
    assert CANARY not in str(info.value)


@pytest.mark.parametrize("configured", [None, SecretStr("")])
def test_a_worker_without_a_configured_voice_refuses_the_marker(configured) -> None:
    with pytest.raises(PermanentProviderError, match="VBEE_VOICE_ID"):
        ConfiguredVoices(configured).resolve(MARKED)


async def test_each_voice_goes_to_its_own_provider() -> None:
    studio, vbee = FakeTts(), FakeTts()
    router = TtsByProvider(studio, vbee)
    await router.synthesize("xin chào", STUDIO)
    await router.synthesize("tạm biệt", MARKED)
    assert (dict(studio.calls), dict(vbee.calls)) == ({"xin chào": 1}, {"tạm biệt": 1})


async def test_a_voice_whose_provider_is_not_configured_fails_for_good() -> None:
    with pytest.raises(PermanentProviderError, match="vbee"):
        await TtsByProvider(FakeTts(), None).synthesize("xin chào", MARKED)
    with pytest.raises(PermanentProviderError, match="voicestudio"):
        await TtsByProvider(None, FakeTts()).synthesize("xin chào", STUDIO)


def acts(store: ObjectStore, vbee: FakeTts, voice_id: str) -> SynthesizeActivities:
    return SynthesizeActivities(
        store,
        TtsByProvider(FakeTts(), vbee),
        model="tts-1",
        resolve=ConfiguredVoices(SecretStr(voice_id)).resolve,
    )


async def test_a_marked_run_is_spoken_in_the_configured_voice_and_hashes_carry_no_id(
    store: ObjectStore,
) -> None:
    key = put_script(store, [segment(0, "xin chào"), segment(1, "tạm biệt")])
    vbee = FakeTts()
    synthesis = acts(store, vbee, CANARY)
    env = ActivityEnvironment()
    plan = await env.run(synthesis.plan_synthesis, vbee_input(), key, None)
    clips = await env.run(synthesis.synthesize_segments, vbee_input(), None, key, list(plan.hashes))
    assert vbee.voices == [CANARY, CANARY]
    kept = plan.model_dump_json() + "".join(c.model_dump_json() for c in clips)
    for clip in clips:
        kept += str(store.metadata(clip.key))
    assert CANARY not in kept


async def test_changing_the_configured_voice_changes_every_clip_hash(store: ObjectStore) -> None:
    key = put_script(store, [segment(0, "xin chào")])
    env = ActivityEnvironment()
    first = await env.run(acts(store, FakeTts(), CANARY).plan_synthesis, vbee_input(), key, None)
    other = await env.run(
        acts(store, FakeTts(), "another-voice").plan_synthesis, vbee_input(), key, None
    )
    assert set(first.hashes).isdisjoint(other.hashes)


async def test_the_studio_voice_hashes_as_it_did_before_the_router(store: ObjectStore) -> None:
    """A run of the other pipeline keeps its cache: its voice is not touched."""
    key = put_script(store, [segment(0, "xin chào")])
    env = ActivityEnvironment()
    routed = await env.run(acts(store, FakeTts(), CANARY).plan_synthesis, make_input(), key, None)
    plain = SynthesizeActivities(store, FakeTts(), model="tts-1")
    assert routed == await env.run(plain.plan_synthesis, make_input(), key, None)


class MemoryRates:
    def __init__(self) -> None:
        self.rows: dict[str, float] = {}

    async def get(self, key: str) -> float | None:
        return self.rows.get(key)

    async def put(self, key: str, rate: float) -> float:
        return self.rows.setdefault(key, rate)


class SampleTts(FakeTts):
    """Reads the sample sentence in 10 s."""

    def length_of(self, text: str) -> float:
        assert text == SAMPLE_TEXT
        return 10.0


async def test_the_rate_of_a_marked_voice_is_measured_in_the_configured_voice_without_engine(
    store: ObjectStore,
) -> None:
    rates, vbee = MemoryRates(), SampleTts()

    def engine() -> str:
        raise AssertionError("VoiceStudio's engine says nothing about a Vbee voice")

    def measure(voice_id: str) -> VoiceRateActivity:
        return VoiceRateActivity(
            TtsByProvider(None, vbee),
            rates,  # type: ignore[arg-type]
            model="tts-1",
            engine=engine,
            resolve=ConfiguredVoices(SecretStr(voice_id)).resolve,
        )

    env = ActivityEnvironment()
    rate = await env.run(measure(CANARY).voice_rate, MARKED)
    assert 5 < rate < 7
    assert vbee.voices == [CANARY]
    # Another configured voice is another voice: measured again, under another key.
    await env.run(measure("another-voice").voice_rate, MARKED)
    assert len(rates.rows) == 2 and vbee.total == 2
    assert not any(CANARY in k for k in rates.rows)
