"""Speaking the script: every distinct text is sent to the provider at most once,
the clip says what it was made from, and a line is judged by how long it takes."""

import asyncio
import base64
import io
import time
import wave
from dataclasses import dataclass

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities import synthesize as synthesize_module
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
from localization.activities.tts_pieces import split_line
from localization.contracts import LocalizationInput
from localization.contracts.voice import VoiceRef
from localization.providers.errors import PermanentProviderError, TransientProviderError
from localization.storage.object_store import ObjectStore, tts_cache_key
from tests.support.inputs import make_input
from tests.support.tts_fakes import FakeTts, make_wav, put_script, segment

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


def test_the_hash_changes_with_the_way_a_clip_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clip read whole is not the clip read piece by piece: a cache made one way must
    not answer for the other."""
    before = _hash()
    monkeypatch.setattr(synthesize_module, "CLIP_METHOD", "another-way")
    assert _hash() != before


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


class SlowTts(FakeTts):
    async def synthesize(self, text, voice):
        await asyncio.sleep(0.2)
        return await super().synthesize(text, voice)


async def test_two_simultaneous_requests_for_one_clip_make_one_provider_call(
    store: ObjectStore,
) -> None:
    """The Dubber's tool calls and a restarted stage can ask for one text at the same
    moment, before either has written the clip: the second waits for the first."""
    tts = SlowTts()
    synth = Synthesizer(store, tts, model="tts-1")
    first, second, third = await asyncio.gather(
        synth.clip(VOICE, None, "xin chào"),
        synth.clip(VOICE, None, "xin chào"),
        synth.clip(VOICE, None, "xin chào"),
    )
    assert first == second == third
    assert tts.total == 1


async def test_simultaneous_requests_for_different_texts_are_not_serialised(
    store: ObjectStore,
) -> None:
    """Control for the test above: the wait is per clip, so two texts still run
    side by side (0.2 s each, well under 0.4 s together)."""
    tts = SlowTts()
    synth = Synthesizer(store, tts, model="tts-1")
    started = time.monotonic()
    await asyncio.gather(synth.clip(VOICE, None, "một"), synth.clip(VOICE, None, "hai"))
    assert time.monotonic() - started < 0.38
    assert tts.total == 2


async def test_a_caller_that_gives_up_does_not_cancel_the_request_the_others_wait_for(
    store: ObjectStore,
) -> None:
    tts = SlowTts()
    synth = Synthesizer(store, tts, model="tts-1")
    impatient = asyncio.ensure_future(synth.clip(VOICE, None, "xin chào"))
    patient = asyncio.ensure_future(synth.clip(VOICE, None, "xin chào"))
    await asyncio.sleep(0.05)
    impatient.cancel()
    clip = await patient
    assert clip.duration_ms > 0
    assert tts.total == 1


async def test_a_failed_request_is_not_remembered_so_the_next_one_tries_again(
    store: ObjectStore,
) -> None:
    tts = FakeTts(fail_with=PermanentProviderError("tts: response is not a WAV file"))
    synth = Synthesizer(store, tts, model="tts-1")
    with pytest.raises(ApplicationError):
        await synth.clip(VOICE, None, "xin chào")
    tts.fail_with = None
    await synth.clip(VOICE, None, "xin chào")
    assert tts.total == 2


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


# --- a long line is read in pieces ----------------------------------------------


LONG_LINE = (
    "Sáu miligam sắt. Mỗi ngày uống ba viên là vừa đủ, dịu nhẹ, dễ hấp thu, "
    "không kích ứng, còn kết hợp thêm peptide hemoglobin và vitamin C."
)


class RecordingTts(FakeTts):
    """Remembers the texts it was asked for, in order."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.asked: list[str] = []

    async def synthesize(self, text, voice):
        self.asked.append(text)
        return await super().synthesize(text, voice)


async def test_a_long_line_is_asked_for_piece_by_piece_and_stored_as_one_clip(
    store: ObjectStore,
) -> None:
    tts = RecordingTts(seconds_per_char=0.05)
    clip = await Synthesizer(store, tts, model="m").clip(VOICE, None, LONG_LINE)
    pieces = split_line(LONG_LINE)
    assert len(pieces) > 1
    assert tts.asked == [p.text for p in pieces]
    assert clip.hash == tts_hash(VOICE, model="m", engine=None, text_vi=LONG_LINE)
    meta = store.metadata(clip.key)
    assert meta is not None
    assert base64.b64decode(meta[META_TEXT]).decode() == LONG_LINE
    for piece in pieces:
        piece_hash = tts_hash(VOICE, model="m", engine=None, text_vi=piece.text)
        assert not store.exists(tts_cache_key(piece_hash))


async def test_the_clip_of_a_long_line_lasts_its_pieces_plus_the_pauses(
    store: ObjectStore,
) -> None:
    tts = RecordingTts(seconds_per_char=0.05)
    clip = await Synthesizer(store, tts, model="m").clip(VOICE, None, LONG_LINE)
    pieces = split_line(LONG_LINE)
    expected = sum(tts.length_of(p.text) * 1000 for p in pieces) + sum(
        p.pause_after_ms for p in pieces
    )
    assert clip.duration_ms == pytest.approx(expected, abs=len(pieces) + 2)
    assert int(store.metadata(clip.key)[META_DURATION]) == clip.duration_ms  # type: ignore[index]


async def test_a_long_line_said_again_is_a_cache_hit_for_the_whole_line(
    store: ObjectStore,
) -> None:
    tts = RecordingTts()
    first = await Synthesizer(store, tts, model="m").clip(VOICE, None, LONG_LINE)
    asked = len(tts.asked)
    second = await Synthesizer(store, tts, model="m").clip(VOICE, None, LONG_LINE)
    assert first == second
    assert len(tts.asked) == asked


async def test_a_piece_is_not_a_cache_entry_of_its_own(store: ObjectStore) -> None:
    """The cache is keyed on the whole line: a piece said on its own is a new clip."""
    tts = RecordingTts()
    synth = Synthesizer(store, tts, model="m")
    await synth.clip(VOICE, None, LONG_LINE)
    assert await synth.cached(VOICE, None, split_line(LONG_LINE)[0].text) is None


def _stereo_wav_at_16k(seconds: float) -> bytes:
    """Audio in a format a join would convert, so a join that ran would show."""
    mono = make_wav(seconds)
    with wave.open(io.BytesIO(mono)) as audio:
        frames = audio.readframes(audio.getnframes())
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"".join(frames[i : i + 2] * 2 for i in range(0, len(frames), 2)))
    return buffer.getvalue()


class StereoTts(RecordingTts):
    async def synthesize(self, text, voice):
        await super().synthesize(text, voice)
        return _stereo_wav_at_16k(self.length_of(text))


async def test_a_short_line_is_still_one_request_and_the_provider_audio_untouched(
    store: ObjectStore,
) -> None:
    tts = StereoTts()
    clip = await Synthesizer(store, tts, model="m").clip(VOICE, None, "Xin chào các bạn.")
    assert tts.asked == ["Xin chào các bạn."]
    assert store.get_bytes(clip.key, produced_by="test") == _stereo_wav_at_16k(
        tts.length_of("Xin chào các bạn.")
    )


class FailsOnSecondPiece(RecordingTts):
    async def synthesize(self, text, voice):
        if self.asked:
            self.asked.append(text)
            raise TransientProviderError("tts: timeout")
        return await super().synthesize(text, voice)


async def test_a_failed_piece_fails_the_clip_as_retryable_and_stores_nothing(
    store: ObjectStore,
) -> None:
    tts = FailsOnSecondPiece()
    with pytest.raises(ApplicationError) as failure:
        await Synthesizer(store, tts, model="m").clip(VOICE, None, LONG_LINE)
    assert not failure.value.non_retryable
    assert len(tts.asked) == 2
    digest = tts_hash(VOICE, model="m", engine=None, text_vi=LONG_LINE)
    assert not store.exists(tts_cache_key(digest))


async def test_the_pieces_of_a_line_are_asked_for_one_at_a_time(store: ObjectStore) -> None:
    """The voice server is one GPU already shared by two activities: a line must not
    add requests running side by side."""
    running = 0
    most = 0

    class Overlap(RecordingTts):
        async def synthesize(self, text, voice):
            nonlocal running, most
            running += 1
            most = max(most, running)
            await asyncio.sleep(0.02)
            running -= 1
            return await super().synthesize(text, voice)

    tts = Overlap()
    await Synthesizer(store, tts, model="m").clip(VOICE, None, LONG_LINE)
    assert len(tts.asked) > 1
    assert most == 1


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


@pytest.mark.parametrize("all_unresolved", [False, True])
async def test_unresolved_is_not_planned_or_sent_to_tts(store, all_unresolved):
    tts = FakeTts()
    acts = activities(store, tts)
    omitted = segment(0, "").model_copy(
        update={"status": "unresolved", "reason": "No recoverable speech"}
    )
    segments = [omitted] if all_unresolved else [omitted, segment(1, "xin chào")]
    key = put_script(store, segments)
    needed = await plan(acts, key)
    assert len(needed.hashes) == (0 if all_unresolved else 1)
    await run_batch(acts, key, list(needed.hashes))
    assert tts.total == (0 if all_unresolved else 1)


# --- a clip far too short for its line --------------------------------------------------

SHORT_LINE = "Tiếc là người Trung Quốc."


@dataclass
class ShortFirst(FakeTts):
    """Answers `short` clips of 287 ms (87 characters a second for the line) first."""

    short: int = 1

    def length_of(self, text: str) -> float:
        if self.calls[text] <= self.short:
            return 0.287
        return super().length_of(text)


def test_a_clip_is_too_short_when_it_is_read_far_faster_than_speech() -> None:
    assert synthesize_module.too_short(SHORT_LINE, 287)
    assert not synthesize_module.too_short(SHORT_LINE, 1250)  # 20 characters a second
    # A line of a word or two can be said quickly.
    assert not synthesize_module.too_short("Vâng.", 100)


async def test_a_short_clip_is_asked_for_again_and_only_the_good_one_is_cached(store) -> None:
    tts = ShortFirst(short=1)
    synth = Synthesizer(store, tts, model="tts-1")
    clip = await synth.clip(VOICE, None, SHORT_LINE)
    assert tts.calls[SHORT_LINE] == 2
    assert clip.duration_ms == pytest.approx(len(SHORT_LINE) * 50, abs=5)
    cached = await synth.cached(VOICE, None, SHORT_LINE)
    assert cached is not None and cached.duration_ms == clip.duration_ms


async def test_a_voice_that_keeps_answering_short_fails_and_caches_nothing(store) -> None:
    tts = ShortFirst(short=synthesize_module.SHORT_CLIP_ATTEMPTS)
    synth = Synthesizer(store, tts, model="tts-1")
    with pytest.raises(ApplicationError) as failure:
        await synth.clip(VOICE, None, SHORT_LINE)
    assert failure.value.type == "ShortClip" and not failure.value.non_retryable
    assert tts.calls[SHORT_LINE] == synthesize_module.SHORT_CLIP_ATTEMPTS
    assert await synth.cached(VOICE, None, SHORT_LINE) is None


async def test_a_short_clip_cached_before_is_spoken_again_and_replaced(store) -> None:
    digest = tts_hash(VOICE, model="tts-1", engine=None, text_vi=SHORT_LINE)
    store.put_bytes(tts_cache_key(digest), make_wav(0.287), metadata={META_DURATION: "287"})
    tts = FakeTts()
    synth = Synthesizer(store, tts, model="tts-1")
    clip = await synth.clip(VOICE, None, SHORT_LINE)
    assert tts.calls[SHORT_LINE] == 1 and clip.duration_ms > 1000
    assert (await synth.cached(VOICE, None, SHORT_LINE)).duration_ms == clip.duration_ms


# --- lines that are not spoken ------------------------------------------------------------


async def test_a_sung_line_is_not_planned_for_synthesis(store) -> None:
    sung = segment(1, "lời bài hát").model_copy(update={"keep_original": True})
    key = put_script(store, [segment(0, "xin chào"), sung])
    acts = activities(store, FakeTts())
    synthesis = await plan(acts, key)
    synth = acts.synthesizer
    assert synthesis.hashes == (synth.hash_of(VOICE, None, "xin chào"),)


# --- A clip is heard back before it is kept -------------------------------------------

from tests.support.tts_fakes import FakeHear  # noqa: E402

WHOLE_LINE = "Mặt thì xinh xắn, dáng thanh mảnh, sao nhảy khỏe thế?"


async def test_a_clip_that_lost_a_clause_is_read_again_clause_by_clause(store):
    tts = FakeTts()
    hear = FakeHear(["Mặt thì xinh xắn sao nhảy khỏe thế", WHOLE_LINE])
    synth = Synthesizer(store, tts, model="tts-1", hear=hear)

    clip = await synth.clip(VOICE, None, WHOLE_LINE)

    assert hear.calls == 2
    # First request: the line whole. Second: each clause on its own.
    assert tts.calls[WHOLE_LINE] == 1
    assert tts.calls["Mặt thì xinh xắn,"] == 1
    assert tts.calls["dáng thanh mảnh,"] == 1
    assert store.metadata(tts_cache_key(clip.hash)) is not None


async def test_a_line_never_heard_whole_keeps_its_best_clip_and_says_what_is_missing(store):
    tts = FakeTts()
    hear = FakeHear(
        [
            "Mặt thì xinh xắn sao nhảy",
            "Mặt thì xinh xắn sao nhảy khỏe thế",
            "Mặt thì xinh xắn sao nhảy",
        ]
    )
    synth = Synthesizer(store, tts, model="tts-1", hear=hear)

    clip = await synth.clip(VOICE, None, WHOLE_LINE)

    assert hear.calls == synthesize_module.SHORT_CLIP_ATTEMPTS
    # The second request lost three words, the first and third five: the second is kept.
    assert clip.lost_words == ("dang", "thanh", "manh")
    stored = await synth.cached(VOICE, None, WHOLE_LINE)
    assert stored is not None and stored.lost_words == ("dang", "thanh", "manh")


async def test_a_clip_kept_with_words_missing_is_read_again_by_the_next_request(store):
    tts = FakeTts()
    hear = FakeHear(["Mặt thì xinh xắn sao nhảy khỏe thế"] * 3 + [WHOLE_LINE])
    synth = Synthesizer(store, tts, model="tts-1", hear=hear)
    first = await synth.clip(VOICE, None, WHOLE_LINE)
    assert first.lost_words

    again = await synth.clip(VOICE, None, WHOLE_LINE)

    assert again.lost_words == ()
    assert (await synth.cached(VOICE, None, WHOLE_LINE)).lost_words == ()


async def test_a_whole_clip_is_heard_once(store):
    hear = FakeHear([WHOLE_LINE])
    synth = Synthesizer(store, FakeTts(), model="tts-1", hear=hear)

    await synth.clip(VOICE, None, WHOLE_LINE)

    assert hear.calls == 1
