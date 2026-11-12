"""How fast a voice speaks, in syllables per second.

`estimate_duration` needs a rate for the voice the video will be dubbed with, and
the only honest source is the voice itself: it reads a sample sentence once, the
audio is measured, and the result is kept under a hash of everything that decides
how the voice sounds. A second video with the same voice reuses the number.
"""

import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path

import psycopg
from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import probe_duration_ms, run_blocking
from localization.contracts.voice import VoiceRef
from localization.providers.base import TtsProvider
from localization.text.vi_syllables import count_syllables

# About sixty syllables once numbers are spoken and Latin words estimated, and it
# has digits, a percentage and a Latin word in it, like the lines it is used to
# predict. Changing the sentence changes every measured rate, so `SAMPLE_VERSION`
# is part of the key.
SAMPLE_TEXT = (
    "Chào các bạn, hôm nay mình sẽ chia sẻ cách chọn mua điện thoại iPhone trong năm 2024. "
    "Giá giảm đến 30% nên rất nhiều người quan tâm, và mình đã thử máy này suốt 15 ngày. "
    "Kết quả khiến mình khá bất ngờ, nên hãy xem hết video này nhé."
)
SAMPLE_VERSION = 1

# A voice whose reading, at its own speed setting 1.0, falls outside this is not one
# the estimate can be trusted for: the audio was truncated, padded with silence, or
# is not the sample at all. The number would be stored for good, so it is refused
# instead. The bound applies to the rate divided by the configured speed, so a voice
# set to read fast (or slow) is judged on how it reads, not on the setting.
MIN_SYLLABLES_PER_SEC = 2.0
MAX_SYLLABLES_PER_SEC = 8.0

SELECT_SQL = "SELECT syllables_per_sec FROM localization.voice_rates WHERE voice_hash = %s"
INSERT_SQL = """
INSERT INTO localization.voice_rates (voice_hash, syllables_per_sec) VALUES (%s, %s)
ON CONFLICT (voice_hash) DO NOTHING
"""


def voice_hash(voice: VoiceRef, *, model: str | None, engine: str | None) -> str:
    """sha256 over the canonical form of everything that decides how the voice
    sounds: provider, model, engine, voice id and settings, and the sample read."""
    canonical = json.dumps(
        {
            "provider": voice.provider,
            "model": model,
            "engine": engine,
            "voice_id": voice.voice_id,
            "settings": voice.settings.model_dump(mode="json"),
            "sample": SAMPLE_VERSION,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def rate_from_audio(wav: bytes) -> float:
    """Syllables per second of the sample read as `wav`."""
    with tempfile.TemporaryDirectory(prefix="rate-") as tmp:
        path = Path(tmp) / "sample.wav"
        path.write_bytes(wav)
        duration_ms = probe_duration_ms(path)
    return count_syllables(SAMPLE_TEXT) / (duration_ms / 1000)


class VoiceRateStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def get(self, key: str) -> float | None:
        async with await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
            row = await (await conn.execute(SELECT_SQL, (key,))).fetchone()
        return None if row is None else float(row[0])

    async def put(self, key: str, rate: float) -> float:
        """Stores `rate` unless another worker stored one first; returns the stored one."""
        async with await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
            await conn.execute(INSERT_SQL, (key, rate))
            row = await (await conn.execute(SELECT_SQL, (key,))).fetchone()
        assert row is not None
        return float(row[0])


class VoiceRateActivity:
    def __init__(
        self,
        tts: TtsProvider,
        store: VoiceRateStore,
        *,
        model: str | None,
        engine: Callable[[], str | None] = lambda: None,
        resolve: Callable[[VoiceRef], VoiceRef] = lambda voice: voice,
    ) -> None:
        self._tts = tts
        self._store = store
        self._model = model
        self._engine = engine
        self._resolve = resolve

    @activity.defn(name="voice_rate")
    async def voice_rate(self, voice: VoiceRef) -> float:
        # The voice a run names becomes the one spoken and hashed (see `tts_router`). The
        # engine is the one VoiceStudio reports; it says nothing about a Vbee or ZeroTTS voice.
        voice = self._resolve(voice)
        engine = None if voice.provider in ("vbee", "zerotts") else self._engine()
        key = voice_hash(voice, model=self._model, engine=engine)
        known = await self._store.get(key)
        if known is not None:
            return known
        with provider_errors():
            wav = await self._tts.synthesize(SAMPLE_TEXT, voice)
        rate = await run_blocking(rate_from_audio, wav)
        at_normal_speed = rate / voice.settings.speed
        if not MIN_SYLLABLES_PER_SEC <= at_normal_speed <= MAX_SYLLABLES_PER_SEC:
            # Retryable, within the workflow's attempt cap: a truncated or silent reply
            # is often a one-off, and a synthesis is cheap. Nothing is stored.
            raise ApplicationError(
                f"the voice read the sample at {rate:.1f} syllables a second "
                f"({at_normal_speed:.1f} at normal speed), outside "
                f"{MIN_SYLLABLES_PER_SEC} to {MAX_SYLLABLES_PER_SEC}; nothing was stored",
                type="ImplausibleVoiceRate",
            )
        return await self._store.put(key, rate)
