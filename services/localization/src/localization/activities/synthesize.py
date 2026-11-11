"""Step 4: the Vietnamese lines become audio clips, cached by what makes them sound.

A clip is identified by a hash of everything that decides how it sounds (provider,
model, engine, voice id, voice settings, the text), and lives at `cache/tts/{hash}.wav`.
A hash is sent to the provider at most once: if the object is there it is reused, and
only a worker killed between the provider's answer and the write can ask again.

This module takes a script and produces audio. It never writes or changes text, so it
must not reach an agent (see tests/architecture/test_single_translation.py).
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import probe_duration_ms, run_blocking
from localization.activities.script_document import read_script
from localization.activities.tts_pieces import Piece, join_pieces, split_line
from localization.contracts import LocalizationInput, SynthClip, SynthesisPlan, VoiceProfile
from localization.contracts.models import STRETCH_MAX, STRETCH_MIN
from localization.contracts.voice import VoiceRef
from localization.observability.tracing import tag_session
from localization.providers.base import TtsProvider
from localization.storage.object_store import ObjectStore, tts_cache_key

# S3 keeps user metadata in one 2 KiB header; the text goes in as base64.
MAX_METADATA_TEXT_BYTES = 1400
BACKGROUND_HEARTBEAT_SECONDS = 5.0

# Metadata keys of a cached clip. Hyphens, because S3 and the proxies in front of it
# fold an underscore in a user-metadata name into a hyphen.
META_TEXT = "text-vi"
META_VOICE = "voice-hash"
META_DURATION = "duration-ms"

BeforePut = Callable[[str], Awaitable[None] | None]

# How a clip is made from its line, part of the clip's cache identity. A line read in one
# request dropped words a line read piece by piece does not, so clips cached the old way
# must not stand in for the new ones.
CLIP_METHOD = "pieces-1"

# A clip read faster than this many characters a second lost most of its words (a 25
# character line came back as 287 ms, 87 a second; lines are read at about 20). It is
# asked for again, and never cached.
MAX_CHARS_PER_SECOND = 45
# A line this short can be said quickly; it is not judged.
MIN_JUDGED_CHARS = 8
# Requests for one line before a clip still too short fails the activity (which Temporal
# retries); a short clip is never stored.
SHORT_CLIP_ATTEMPTS = 3


def too_short(text_vi: str, duration_ms: int) -> bool:
    """Whether a clip of `duration_ms` is far shorter than `text_vi` can be read in."""
    chars = sum(not c.isspace() for c in text_vi)
    return chars >= MIN_JUDGED_CHARS and chars * 1000 > MAX_CHARS_PER_SECOND * duration_ms


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def voice_identity_hash(voice: VoiceRef, *, model: str | None, engine: str | None) -> str:
    """Everything about how a voice sounds except what it says."""
    return hashlib.sha256(
        _canonical(
            {
                "provider": voice.provider,
                "model": model,
                "engine": engine,
                "voice_id": voice.voice_id,
                "settings": voice.settings.model_dump(mode="json"),
            }
        ).encode()
    ).hexdigest()


def tts_hash(voice: VoiceRef, *, model: str | None, engine: str | None, text_vi: str) -> str:
    """The cache identity of one clip: the voice's identity, the text and how the clip
    is made from it."""
    return hashlib.sha256(
        _canonical(
            {
                "method": CLIP_METHOD,
                "provider": voice.provider,
                "model": model,
                "engine": engine,
                "voice_id": voice.voice_id,
                "settings": voice.settings.model_dump(mode="json"),
                "text_vi": text_vi,
            }
        ).encode()
    ).hexdigest()


def needed_stretch(duration_ms: int, slot_ms: int) -> float:
    """How much faster than natural the clip must play to fit its slot."""
    return duration_ms / max(slot_ms, 1)


def stretch_for(duration_ms: int, slot_ms: int) -> float | None:
    """1.0 for a clip that fits, the exact ratio for one that fits once sped up by
    at most STRETCH_MAX, and None for a clip that cannot be made to fit: that one
    is for the Dubber to shorten."""
    ratio = needed_stretch(duration_ms, slot_ms)
    if ratio <= 1.0:
        return 1.0
    if ratio <= STRETCH_MAX:
        return ratio
    return None


def final_stretch(duration_ms: int, slot_ms: int, chosen: float | None) -> tuple[float, bool]:
    """The ratio the clip is played at and whether it still overflows its slot.
    The Dubber's choice is honoured unless it would leave the clip longer than the
    slot it could have fitted."""
    needed = needed_stretch(duration_ms, slot_ms)
    wanted = max(1.0 if chosen is None else chosen, min(needed, STRETCH_MAX))
    return min(max(wanted, STRETCH_MIN), STRETCH_MAX), needed > STRETCH_MAX


@dataclass(frozen=True)
class Clip:
    hash: str
    key: str
    duration_ms: int


def wav_duration_ms(wav: bytes) -> int:
    with tempfile.TemporaryDirectory(prefix="clip-") as tmp:
        path = Path(tmp) / "clip.wav"
        path.write_bytes(wav)
        return probe_duration_ms(path)


@contextlib.asynccontextmanager
async def background_heartbeat(interval: float = BACKGROUND_HEARTBEAT_SECONDS):
    """Heartbeats while the body runs, so a long provider call is not mistaken for
    a dead worker and a dead worker is noticed in seconds."""

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            activity.heartbeat()

    task = asyncio.ensure_future(beat())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _as_given(voice: VoiceRef) -> VoiceRef:
    return voice


class Synthesizer:
    """Reads a clip from the cache or asks the provider for it. Shared by the batch
    activity and the Dubber's tool, so a text synthesized by either is a cache hit
    for the other."""

    def __init__(
        self,
        store: ObjectStore,
        tts: TtsProvider,
        *,
        model: str | None,
        before_put: BeforePut | None = None,
        resolve: Callable[[VoiceRef], VoiceRef] = _as_given,
    ) -> None:
        self._store = store
        self._tts = tts
        self._model = model
        self._before_put = before_put
        # The voice a run names becomes the one spoken and hashed (see `tts_router`).
        self._resolve = resolve
        self._in_flight: dict[str, asyncio.Future[Clip]] = {}

    def hash_of(self, voice: VoiceRef, engine: str | None, text_vi: str) -> str:
        return tts_hash(self._resolve(voice), model=self._model, engine=engine, text_vi=text_vi)

    async def cached(self, voice: VoiceRef, engine: str | None, text_vi: str) -> Clip | None:
        digest = self.hash_of(voice, engine, text_vi)
        return await self._cached(digest)

    async def _cached(self, digest: str) -> Clip | None:
        key = tts_cache_key(digest)
        meta = await asyncio.to_thread(self._store.metadata, key)
        if meta is None:
            return None
        if META_DURATION in meta:
            return Clip(digest, key, int(meta[META_DURATION]))
        # An object without a recorded length: measure what is there.
        wav = await asyncio.to_thread(self._store.get_bytes, key, produced_by="synthesize")
        return Clip(digest, key, await run_blocking(wav_duration_ms, wav))

    async def clip(self, voice: VoiceRef, engine: str | None, text_vi: str) -> Clip:
        """The clip of `text_vi`. Callers in this process that want the same clip at
        the same time share one request: the second waits for the first instead of
        asking the provider again. (Another process cannot be seen from here; two
        workers speaking one new text at once can both ask. That costs a call and
        nothing else: the clip is the same and is written whole.)"""
        voice = self._resolve(voice)
        digest = tts_hash(voice, model=self._model, engine=engine, text_vi=text_vi)
        flight = self._in_flight.get(digest)
        if flight is None:
            flight = asyncio.ensure_future(self._clip(digest, voice, engine, text_vi))
            self._in_flight[digest] = flight
            flight.add_done_callback(lambda done: self._landed(digest, done))
        # Shielded: a caller that is cancelled must not cancel the request the
        # others are waiting for.
        return await asyncio.shield(flight)

    def _landed(self, digest: str, flight: "asyncio.Future[Clip]") -> None:
        self._in_flight.pop(digest, None)
        if not flight.cancelled():
            flight.exception()  # retrieved: a failure nobody waits for is not a warning

    async def _clip(self, digest: str, voice: VoiceRef, engine: str | None, text_vi: str) -> Clip:
        known = await self._cached(digest)
        # A clip cached before short ones were refused is spoken again, and replaced.
        if known is not None and not too_short(text_vi, known.duration_ms):
            return known
        encoded = base64.b64encode(text_vi.encode()).decode()
        if len(encoded) > MAX_METADATA_TEXT_BYTES:
            raise ApplicationError(
                "the line is too long to be recorded with its clip",
                type="TextTooLong",
                non_retryable=True,
            )
        for attempt in range(1, SHORT_CLIP_ATTEMPTS + 1):
            wav = await self._speak(voice, text_vi)
            duration_ms = await run_blocking(wav_duration_ms, wav)
            if not too_short(text_vi, duration_ms):
                break
            activity.logger.warning(
                "clip %s is %d ms for %d characters (attempt %d): refused",
                digest[:12],
                duration_ms,
                len(text_vi),
                attempt,
            )
        else:
            raise ApplicationError(
                f"the voice returned clips too short for the line {SHORT_CLIP_ATTEMPTS} times",
                type="ShortClip",
            )
        if self._before_put is not None:
            outcome = self._before_put(digest)
            if outcome is not None:
                await outcome
        key = tts_cache_key(digest)
        await asyncio.to_thread(
            self._store.put_bytes,
            key,
            wav,
            content_type="audio/wav",
            metadata={
                META_TEXT: encoded,
                META_VOICE: voice_identity_hash(voice, model=self._model, engine=engine),
                META_DURATION: str(duration_ms),
            },
        )
        return Clip(digest, key, duration_ms)

    async def _speak(self, voice: VoiceRef, text_vi: str) -> bytes:
        """The audio of a line, read piece by piece (see tts_pieces) and joined. A
        line of one piece is the provider's audio as it came. The pieces are asked
        for one after another: the voice server is one GPU, already shared by the
        activities running side by side. Any piece failing fails the line."""
        pieces = split_line(text_vi) or [Piece(text_vi, 0)]
        with provider_errors():
            wavs = [await self._tts.synthesize(piece.text, voice) for piece in pieces]
            if len(wavs) == 1:
                return wavs[0]
            return await run_blocking(join_pieces, wavs, [p.pause_after_ms for p in pieces[:-1]])


def spoken(segment) -> bool:
    """Whether a script segment is read by the voice: not omitted, not sung."""
    return segment.status != "unresolved" and not segment.keep_original


def segment_hashes(
    synth: Synthesizer, voice: VoiceRef, engine: str | None, script
) -> dict[str, str]:
    """Hash -> text for every distinct text in `script` that is spoken, in order of first
    use. An omitted line and a sung one (its original sound is kept) are not spoken."""
    texts: dict[str, str] = {}
    for segment in script.segments:
        if not spoken(segment):
            continue
        texts.setdefault(synth.hash_of(voice, engine, segment.text_vi), segment.text_vi)
    return texts


class SynthesizeActivities:
    def __init__(
        self,
        store: ObjectStore,
        tts: TtsProvider,
        *,
        model: str | None,
        engine: Callable[[], str | None] = lambda: None,
        before_put: BeforePut | None = None,
        resolve: Callable[[VoiceRef], VoiceRef] = _as_given,
    ) -> None:
        self._store = store
        self._synth = Synthesizer(store, tts, model=model, before_put=before_put, resolve=resolve)
        self._engine = engine

    @property
    def synthesizer(self) -> Synthesizer:
        return self._synth

    @activity.defn(name="voice_profile")
    async def voice_profile(self, voice: VoiceRef) -> VoiceProfile:
        return VoiceProfile(engine=self._engine())

    @activity.defn(name="plan_synthesis")
    async def plan_synthesis(
        self, spec: LocalizationInput, script_key: str, engine: str | None
    ) -> SynthesisPlan:
        tag_session(spec.aweme_id)
        script = await asyncio.to_thread(read_script, self._store, script_key, spec)
        # One hash is one provider call, so a text said twice is planned once.
        hashes = segment_hashes(self._synth, spec.voice, engine, script)
        return SynthesisPlan(hashes=tuple(hashes))

    @activity.defn(name="synthesize_segments")
    async def synthesize_segments(
        self,
        spec: LocalizationInput,
        engine: str | None,
        script_key: str,
        hashes: list[str],
    ) -> list[SynthClip]:
        tag_session(spec.aweme_id)
        script = await asyncio.to_thread(read_script, self._store, script_key, spec)
        texts = segment_hashes(self._synth, spec.voice, engine, script)
        clips: list[SynthClip] = []
        async with background_heartbeat():
            for digest in hashes:
                if digest not in texts:
                    raise ApplicationError(
                        "a hash was asked for that no line of the script has",
                        type="UnknownHash",
                        non_retryable=True,
                    )
                clip = await self._synth.clip(spec.voice, engine, texts[digest])
                clips.append(
                    SynthClip(hash=digest, key=clip.key, duration_ms=max(1, clip.duration_ms))
                )
                activity.heartbeat(digest)
        return clips
