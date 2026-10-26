"""Measuring how fast a voice speaks: once per voice, keyed by everything that
changes how it sounds."""

import asyncio
import io
import wave

import psycopg
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.voice_rate import (
    SAMPLE_TEXT,
    VoiceRateActivity,
    VoiceRateStore,
    voice_hash,
)
from localization.contracts.voice import VoiceRef
from localization.providers.errors import PermanentProviderError, TransientProviderError
from localization.text.vi_syllables import count_syllables


def wav_of(seconds: float) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * round(16000 * seconds))
    return out.getvalue()


class FakeTts:
    def __init__(self, seconds: float = 12.4, error: Exception | None = None) -> None:
        self.seconds = seconds
        self.error = error
        self.texts: list[str] = []
        self.voices: list[VoiceRef] = []

    async def synthesize(self, text: str, voice: VoiceRef) -> bytes:
        self.texts.append(text)
        self.voices.append(voice)
        await asyncio.sleep(0.05)
        if self.error is not None:
            raise self.error
        return wav_of(self.seconds)


def voice(voice_id: str = "voice-a", speed: float = 1.0) -> VoiceRef:
    return VoiceRef.model_validate(
        {"provider": "voicestudio", "voice_id": voice_id, "settings": {"speed": speed}}
    )


def activity_for(tts: FakeTts, conninfo: str, *, model="tts-1", engine=None) -> VoiceRateActivity:
    return VoiceRateActivity(tts, VoiceRateStore(conninfo), model=model, engine=lambda: engine)


def stored(conn: psycopg.Connection) -> list[tuple[str, float]]:
    return conn.execute(
        "SELECT voice_hash, syllables_per_sec FROM localization.voice_rates"
    ).fetchall()


def test_the_sample_is_about_sixty_syllables_and_has_digits_and_a_latin_word() -> None:
    assert 55 <= count_syllables(SAMPLE_TEXT) <= 70
    assert any(ch.isdigit() for ch in SAMPLE_TEXT)
    assert "iPhone" in SAMPLE_TEXT


async def test_the_rate_is_the_syllables_of_the_sample_over_the_seconds_it_took(
    glossary_db,
) -> None:
    info, conn = glossary_db
    tts = FakeTts(seconds=12.4)
    rate = await ActivityEnvironment().run(activity_for(tts, info).voice_rate, voice())
    assert rate == pytest.approx(count_syllables(SAMPLE_TEXT) / 12.4, rel=0.01)
    assert tts.texts == [SAMPLE_TEXT]
    assert len(stored(conn)) == 1


async def test_a_voice_already_measured_is_not_synthesised_again(glossary_db) -> None:
    info, conn = glossary_db
    tts = FakeTts()
    act = activity_for(tts, info)
    first = await ActivityEnvironment().run(act.voice_rate, voice())
    second = await ActivityEnvironment().run(act.voice_rate, voice())
    assert first == second
    assert len(tts.texts) == 1
    assert len(stored(conn)) == 1


async def test_another_worker_with_the_same_voice_reuses_the_stored_rate(glossary_db) -> None:
    info, _ = glossary_db
    await ActivityEnvironment().run(activity_for(FakeTts(10), info).voice_rate, voice())
    other = FakeTts(seconds=20)
    rate = await ActivityEnvironment().run(activity_for(other, info).voice_rate, voice())
    assert other.texts == []
    assert rate == pytest.approx(count_syllables(SAMPLE_TEXT) / 10, rel=0.01)


@pytest.mark.parametrize(
    ("other_voice", "model", "engine"),
    [
        (voice("voice-b"), "tts-1", None),
        (voice(speed=1.5), "tts-1", None),
        (voice(), "tts-2", None),
        (voice(), "tts-1", "engine-x"),
    ],
    ids=["voice id", "settings", "model", "engine"],
)
async def test_anything_that_changes_how_the_voice_sounds_is_measured_again(
    glossary_db, other_voice: VoiceRef, model: str, engine: str | None
) -> None:
    info, conn = glossary_db
    await ActivityEnvironment().run(activity_for(FakeTts(), info).voice_rate, voice())
    again = FakeTts()
    await ActivityEnvironment().run(
        activity_for(again, info, model=model, engine=engine).voice_rate, other_voice
    )
    assert len(again.texts) == 1
    assert len(stored(conn)) == 2


def test_the_key_is_stable_and_names_every_input() -> None:
    base = voice_hash(voice(), model="tts-1", engine="e")
    assert base == voice_hash(voice(), model="tts-1", engine="e")
    assert (
        len(
            {
                base,
                voice_hash(voice("v2"), model="tts-1", engine="e"),
                voice_hash(voice(), model="tts-2", engine="e"),
                voice_hash(voice(), model="tts-1", engine="f"),
                voice_hash(voice(speed=2.0), model="tts-1", engine="e"),
            }
        )
        == 5
    )


async def test_a_provider_outage_is_retryable_and_stores_nothing(glossary_db) -> None:
    info, conn = glossary_db
    act = activity_for(FakeTts(error=TransientProviderError("tts: HTTP 503")), info)
    with pytest.raises(ApplicationError) as err:
        await ActivityEnvironment().run(act.voice_rate, voice())
    assert not err.value.non_retryable
    assert stored(conn) == []


async def test_a_refusal_is_not_retried(glossary_db) -> None:
    info, _ = glossary_db
    act = activity_for(FakeTts(error=PermanentProviderError("tts: HTTP 400")), info)
    with pytest.raises(ApplicationError) as err:
        await ActivityEnvironment().run(act.voice_rate, voice())
    assert err.value.non_retryable


async def test_two_workers_measuring_a_new_voice_at_once_agree_on_one_rate(glossary_db) -> None:
    info, conn = glossary_db
    first, second = FakeTts(10), FakeTts(20)
    rates = await asyncio.gather(
        ActivityEnvironment().run(activity_for(first, info).voice_rate, voice()),
        ActivityEnvironment().run(activity_for(second, info).voice_rate, voice()),
    )
    assert rates[0] == rates[1]
    assert len(stored(conn)) == 1


@pytest.mark.parametrize("seconds", [0.5, 3.0, 60.0], ids=["near-empty", "truncated", "padded"])
async def test_an_implausible_reading_is_refused_and_never_stored(
    glossary_db, seconds: float
) -> None:
    info, conn = glossary_db
    act = activity_for(FakeTts(seconds=seconds), info)
    with pytest.raises(ApplicationError) as err:
        await ActivityEnvironment().run(act.voice_rate, voice())
    assert err.value.type == "ImplausibleVoiceRate"
    assert err.value.non_retryable
    assert stored(conn) == []


async def test_a_reading_at_the_edge_of_the_range_is_kept(glossary_db) -> None:
    info, conn = glossary_db
    seconds = count_syllables(SAMPLE_TEXT) / 7.9
    await ActivityEnvironment().run(activity_for(FakeTts(seconds), info).voice_rate, voice())
    assert len(stored(conn)) == 1
