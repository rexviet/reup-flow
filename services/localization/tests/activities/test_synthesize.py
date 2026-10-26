"""Speaking the script: every distinct text is sent to the provider at most once,
the clip says what it was made from, and a line is judged by how long it takes."""

import base64

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.script_document import read_script
from localization.activities.synthesize import (
    META_DURATION,
    META_TEXT,
    META_VOICE,
    SynthesizeActivities,
    Synthesizer,
    final_stretch,
    stretch_for,
    tts_hash,
    voice_identity_hash,
)
from localization.contracts import LocalizationInput
from localization.contracts.voice import VoiceRef
from localization.providers.errors import PermanentProviderError
from localization.storage.object_store import ObjectStore, tts_cache_key
from tests.support.inputs import make_input
from tests.support.tts_fakes import FakeTts, put_script, segment

SPEC = make_input()
VOICE = SPEC.voice


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        yield ObjectStore(client, "reup-media")


def activities(store, tts, *, model="tts-1", engine=None, before_put=None) -> SynthesizeActivities:
    return SynthesizeActivities(
        store, tts, model=model, engine=lambda: engine, before_put=before_put
    )


async def run_batch(acts, script_key, hashes, *, spec=SPEC, engine=None, beats=None):
    env = ActivityEnvironment()
    if beats is not None:
        env.on_heartbeat = lambda *details: beats.append(details)
    return await env.run(acts.synthesize_segments, spec, engine, script_key, hashes)


async def plan(acts, script_key, *, spec=SPEC, engine=None):
    return await ActivityEnvironment().run(acts.plan_synthesis, spec, script_key, engine)


# --- the hash -------------------------------------------------------------------


def _voice(**over) -> VoiceRef:
    body = {"provider": "voicestudio", "voice_id": "voice-a", "settings": {}}
    body.update(over)
    return VoiceRef.model_validate(body)


def _hash(voice=None, model="m", engine="e", text="xin chào") -> str:
    return tts_hash(voice or _voice(), model=model, engine=engine, text_vi=text)


def test_the_hash_is_stable_for_the_same_inputs() -> None:
    assert _hash() == _hash()
    assert len(_hash()) == 64


@pytest.mark.parametrize(
    "changed",
    [
        {"model": "other-model"},
        {"engine": "other-engine"},
        {"engine": None},
        {"text": "xin chào."},
        {"voice": _voice(voice_id="voice-b")},
        {"voice": _voice(settings={"speed": 1.2})},
        {"voice": _voice(settings={"instructions": "calm"})},
        {"voice": _voice(provider="openai_compat")},
    ],
    ids=lambda c: next(iter(c)) + ":" + str(next(iter(c.values())))[:20],
)
def test_every_part_of_how_a_clip_sounds_changes_its_hash(changed) -> None:
    assert _hash(**changed) != _hash()


def test_the_voice_identity_leaves_out_only_the_text() -> None:
    assert voice_identity_hash(_voice(), model="m", engine="e") == voice_identity_hash(
        _voice(), model="m", engine="e"
    )
    assert voice_identity_hash(_voice(), model="m", engine="e") != voice_identity_hash(
        _voice(), model="m2", engine="e"
    )


# --- the cache ------------------------------------------------------------------


async def test_a_clip_is_stored_with_the_text_it_was_made_from(store: ObjectStore) -> None:
    tts = FakeTts(seconds_per_char=0.1)
    synth = Synthesizer(store, tts, model="tts-1")
    clip = await synth.clip(VOICE, "eng", "xin chào các bạn")
    meta = store.metadata(tts_cache_key(clip.hash))
    assert meta is not None
    assert base64.b64decode(meta[META_TEXT]).decode() == "xin chào các bạn"
    assert meta[META_VOICE] == voice_identity_hash(VOICE, model="tts-1", engine="eng")
    assert int(meta[META_DURATION]) == clip.duration_ms
    # 16 characters at 0.1 s each: the length is measured from the audio, not assumed.
    assert clip.duration_ms == pytest.approx(1600, abs=30)
    assert clip.key == tts_cache_key(clip.hash)


async def test_text_with_accents_and_emoji_survives_the_metadata_round_trip(
    store: ObjectStore,
) -> None:
    text = "Đây là một câu có dấu, “trích dẫn” và biểu tượng 😀"
    clip = await Synthesizer(store, FakeTts(), model="m").clip(VOICE, None, text)
    meta = store.metadata(clip.key)
    assert meta is not None
    assert base64.b64decode(meta[META_TEXT]).decode() == text


async def test_a_text_that_is_already_cached_is_not_sent_to_the_provider_again(
    store: ObjectStore,
) -> None:
    tts = FakeTts()
    synth = Synthesizer(store, tts, model="tts-1")
    first = await synth.clip(VOICE, None, "xin chào")
    second = await synth.clip(VOICE, None, "xin chào")
    assert first == second
    assert tts.total == 1


async def test_a_second_worker_finds_the_clip_the_first_one_made(store: ObjectStore) -> None:
    tts = FakeTts()
    await Synthesizer(store, tts, model="tts-1").clip(VOICE, None, "xin chào")
    await Synthesizer(store, tts, model="tts-1").clip(VOICE, None, "xin chào")
    assert tts.total == 1


@pytest.mark.parametrize(
    ("model_b", "engine_b"), [("tts-2", None), ("tts-1", "other-engine")], ids=["model", "engine"]
)
async def test_changing_the_model_or_the_engine_is_a_cache_miss(
    store: ObjectStore, model_b: str, engine_b: str | None
) -> None:
    tts = FakeTts()
    a = await Synthesizer(store, tts, model="tts-1").clip(VOICE, None, "xin chào")
    b = await Synthesizer(store, tts, model=model_b).clip(VOICE, engine_b, "xin chào")
    assert a.hash != b.hash
    assert tts.total == 2
    assert store.exists(a.key) and store.exists(b.key)


async def test_nothing_is_cached_when_the_provider_fails(store: ObjectStore) -> None:
    tts = FakeTts(fail_with=PermanentProviderError("tts: response is not a WAV file"))
    synth = Synthesizer(store, tts, model="tts-1")
    with pytest.raises(ApplicationError) as failure:
        await synth.clip(VOICE, None, "xin chào")
    assert failure.value.non_retryable
    digest = tts_hash(VOICE, model="tts-1", engine=None, text_vi="xin chào")
    assert not store.exists(tts_cache_key(digest))


async def test_a_line_too_long_for_its_metadata_fails_before_the_provider_is_paid(
    store: ObjectStore,
) -> None:
    tts = FakeTts()
    with pytest.raises(ApplicationError) as failure:
        await Synthesizer(store, tts, model="m").clip(VOICE, None, "dài " * 600)
    assert failure.value.type == "TextTooLong"
    assert tts.total == 0


async def test_the_object_is_not_there_when_the_before_put_hook_runs(store: ObjectStore) -> None:
    """The window between the provider's answer and the write: a kill there loses
    the clip, so it must be the only thing that can."""
    seen: list[tuple[str, bool]] = []

    def hook(digest: str) -> None:
        seen.append((digest, store.exists(tts_cache_key(digest))))

    clip = await Synthesizer(store, FakeTts(), model="m", before_put=hook).clip(
        VOICE, None, "xin chào"
    )
    assert seen == [(clip.hash, False)]
    assert store.exists(clip.key)


# --- the activities -------------------------------------------------------------


async def test_the_plan_lists_a_text_said_twice_once(store: ObjectStore) -> None:
    key = put_script(
        store,
        [segment(0, "xin chào"), segment(1, "tạm biệt"), segment(2, "xin chào"), segment(3, "ừ")],
    )
    planned = await plan(activities(store, FakeTts()), key)
    assert len(planned.hashes) == 3
    assert len(set(planned.hashes)) == 3


async def test_a_script_with_two_segments_of_the_same_text_makes_one_provider_call(
    store: ObjectStore,
) -> None:
    tts = FakeTts()
    acts = activities(store, tts)
    key = put_script(
        store,
        [segment(0, "xin chào"), segment(1, "tạm biệt"), segment(2, "xin chào")],
    )
    planned = await plan(acts, key)
    await run_batch(acts, key, list(planned.hashes))
    assert tts.calls == {"xin chào": 1, "tạm biệt": 1}


async def test_every_batch_of_a_dedup_plan_together_calls_each_text_once(
    store: ObjectStore,
) -> None:
    """The same text in two different batches is the case a per-batch check misses:
    only the plan can prevent it, and it must."""
    tts = FakeTts()
    acts = activities(store, tts)
    texts = [f"câu số {i}" for i in range(12)] + ["câu số 0", "câu số 11"]
    key = put_script(store, [segment(i, t) for i, t in enumerate(texts)])
    planned = await plan(acts, key)
    hashes = list(planned.hashes)
    for i in range(0, len(hashes), 10):
        await run_batch(acts, key, hashes[i : i + 10])
    assert tts.total == 12
    assert max(tts.calls.values()) == 1


async def test_a_batch_returns_the_clip_and_the_length_of_every_hash(store: ObjectStore) -> None:
    tts = FakeTts(seconds_per_char=0.1)
    acts = activities(store, tts)
    key = put_script(store, [segment(0, "một hai ba"), segment(1, "bốn năm")])
    planned = await plan(acts, key)
    clips = await run_batch(acts, key, list(planned.hashes))
    assert [c.hash for c in clips] == list(planned.hashes)
    assert [c.key for c in clips] == [tts_cache_key(h) for h in planned.hashes]
    assert clips[0].duration_ms == pytest.approx(1000, abs=30)
    assert clips[1].duration_ms == pytest.approx(700, abs=30)


async def test_a_batch_heartbeats_after_each_hash(store: ObjectStore) -> None:
    acts = activities(store, FakeTts())
    key = put_script(store, [segment(i, f"câu {i}") for i in range(4)])
    planned = await plan(acts, key)
    beats: list[tuple] = []
    await run_batch(acts, key, list(planned.hashes), beats=beats)
    after_each = [b[0] for b in beats if b]
    assert after_each == list(planned.hashes)


async def test_a_hash_no_line_has_is_refused_without_calling_the_provider(
    store: ObjectStore,
) -> None:
    tts = FakeTts()
    key = put_script(store, [segment(0, "xin chào")])
    with pytest.raises(ApplicationError) as failure:
        await run_batch(activities(store, tts), key, ["f" * 64])
    assert failure.value.type == "UnknownHash"
    assert failure.value.non_retryable
    assert tts.total == 0


async def test_the_hash_of_a_batch_uses_the_engine_the_run_recorded(store: ObjectStore) -> None:
    """The engine comes from the workflow, not from the worker's current health
    state: a worker that sees a different engine on resume must not change the keys."""
    tts = FakeTts()
    acts = activities(store, tts, engine="engine-now")
    key = put_script(store, [segment(0, "xin chào")])
    planned = await plan(acts, key, engine="engine-recorded")
    expected = tts_hash(VOICE, model="tts-1", engine="engine-recorded", text_vi="xin chào")
    assert planned.hashes == (expected,)
    await run_batch(acts, key, list(planned.hashes), engine="engine-recorded")
    assert store.exists(tts_cache_key(expected))


async def test_the_voice_profile_reports_the_active_engine() -> None:
    acts = SynthesizeActivities(object(), FakeTts(), model="m", engine=lambda: "omni")  # type: ignore[arg-type]
    profile = await ActivityEnvironment().run(acts.voice_profile, VOICE)
    assert profile.engine == "omni"


# --- how a line is judged -------------------------------------------------------


@pytest.mark.parametrize(
    ("duration", "slot", "expected"),
    [
        (500, 1000, 1.0),
        (1000, 1000, 1.0),
        (1001, 1000, 1.001),
        (1100, 1000, 1.1),
        (1150, 1000, 1.15),
        (1151, 1000, None),
        (2000, 1000, None),
        (2, 0, None),
    ],
)
def test_the_stretch_table(duration: int, slot: int, expected: float | None) -> None:
    got = stretch_for(duration, slot)
    assert got == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize(
    ("duration", "slot", "chosen", "ratio", "overflow"),
    [
        (800, 1000, None, 1.0, False),
        (1100, 1000, None, 1.1, False),
        (1100, 1000, 1.15, 1.15, False),
        (1100, 1000, 1.05, 1.1, False),
        (2000, 1000, 1.15, 1.15, True),
        (2000, 1000, None, 1.15, True),
        (500, 1000, 0.85, 0.85, False),
        (950, 1000, 0.85, 0.95, False),
    ],
)
def test_the_final_stretch_never_leaves_a_clip_longer_than_it_has_to_be(
    duration: int, slot: int, chosen: float | None, ratio: float, overflow: bool
) -> None:
    got, flagged = final_stretch(duration, slot, chosen)
    assert got == pytest.approx(ratio)
    assert flagged is overflow


async def test_a_script_read_back_is_the_one_that_was_written(store: ObjectStore) -> None:
    key = put_script(store, [segment(0, "xin chào"), segment(1, "tạm biệt")])
    script = read_script(store, key)
    assert [s.text_vi for s in script.segments] == ["xin chào", "tạm biệt"]


def test_the_input_is_the_one_every_test_here_shares() -> None:
    assert isinstance(SPEC, LocalizationInput)
