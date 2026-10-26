"""A TTS provider that returns real WAV audio and counts what it was asked, and the
script fixtures the dubbing tests share."""

import io
import math
import struct
import wave
from collections import Counter
from dataclasses import dataclass, field

from localization.activities.script_document import script_key, write_script
from localization.contracts import LocalizedScript, LocalizedSegment
from localization.contracts.voice import VoiceRef
from localization.storage.object_store import ObjectStore

SAMPLE_RATE = 24000


def make_wav(seconds: float, *, frequency: float = 220.0, amplitude: float = 0.3) -> bytes:
    """A mono 16-bit WAV of `seconds`, a tone: ffprobe measures it like any speech."""
    frames = max(1, round(seconds * SAMPLE_RATE))
    samples = (
        struct.pack(
            "<h", round(32767 * amplitude * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE))
        )
        for i in range(frames)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(b"".join(samples))
    return buffer.getvalue()


def make_clicks(seconds: float, *, amplitude: float = 0.95) -> bytes:
    """A mono 16-bit WAV of short loud clicks: a high peak for little loudness, the
    shape that exposes a missing true-peak ceiling."""
    frames = max(1, round(seconds * SAMPLE_RATE))
    samples = (
        struct.pack("<h", round(32767 * amplitude) if i % 2400 < 30 else 0) for i in range(frames)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(b"".join(samples))
    return buffer.getvalue()


@dataclass
class FakeTts:
    """Reads a line in `seconds_per_char` seconds per character (at least 0.2 s).
    `calls` counts provider calls by text; `fail_with` is raised instead of
    answering."""

    seconds_per_char: float = 0.05
    calls: Counter = field(default_factory=Counter)
    voices: list[str] = field(default_factory=list)
    fail_with: Exception | None = None

    def length_of(self, text: str) -> float:
        return max(0.2, len(text) * self.seconds_per_char)

    async def synthesize(self, text: str, voice: VoiceRef) -> bytes:
        self.calls[text] += 1
        self.voices.append(voice.voice_id)
        if self.fail_with is not None:
            raise self.fail_with
        return make_wav(self.length_of(text))

    @property
    def total(self) -> int:
        return sum(self.calls.values())


def segment(
    i: int, text: str, *, slot_ms: int = 4000, start_ms: int | None = None
) -> LocalizedSegment:
    start = i * 5000 if start_ms is None else start_ms
    return LocalizedSegment(
        id=i,
        start_ms=start,
        end_ms=start + slot_ms,
        slot_ms=slot_ms,
        src_zh="你好",
        text_vi=text,
        on_screen=False,
    )


def put_script(
    store: ObjectStore,
    segments: list[LocalizedSegment],
    *,
    aweme_id: str = "7001",
    version: int = 1,
) -> str:
    script = LocalizedScript(
        aweme_id=aweme_id, version=version, prompt_version="test", segments=tuple(segments)
    )
    write_script(store, script)
    return script_key(aweme_id, version)
