"""Speech-boundary WAV chunks for independent language detection before STT."""

import io
import wave
from dataclasses import dataclass

from localization.providers.errors import PermanentProviderError

SAMPLE_RATE = 16000
MAX_CHUNK_SAMPLES = 12 * SAMPLE_RATE
PADDING_SAMPLES = SAMPLE_RATE // 5
# Expressive/brief dialogue on a separated stem can fall below Silero's 0.5 default.
# Keep it eligible for ASR; its language/text still require downstream evidence.
VAD_SPEECH_THRESHOLD = 0.25


@dataclass(frozen=True)
class SpeechChunk:
    start_ms: int
    end_ms: int
    audio: bytes


def speech_spans(pcm: bytes) -> list[dict[str, int]]:
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    samples = torch.frombuffer(bytearray(pcm), dtype=torch.int16).float() / 32768
    return get_speech_timestamps(
        samples,
        load_silero_vad(),
        sampling_rate=SAMPLE_RATE,
        threshold=VAD_SPEECH_THRESHOLD,
        max_speech_duration_s=10,
        min_silence_duration_ms=350,
    )


def chunk_bounds(spans: list[dict[str, int]], samples: int) -> list[tuple[int, int]]:
    """Merge nearby speech, pad edges and resolve padding overlap without duplication."""
    bounds: list[tuple[int, int]] = []
    for span in spans:
        start = max(0, span["start"] - PADDING_SAMPLES)
        end = min(samples, span["end"] + PADDING_SAMPLES)
        if start >= end:
            continue
        if (
            bounds
            and end - bounds[-1][0] <= MAX_CHUNK_SAMPLES
            and start - bounds[-1][1] <= SAMPLE_RATE
        ):
            bounds[-1] = (bounds[-1][0], end)
        else:
            if bounds and start < bounds[-1][1]:
                midpoint = (start + bounds[-1][1]) // 2
                bounds[-1], start = (bounds[-1][0], midpoint), midpoint
            # Bound even an unexpectedly long span from the detector.
            while end - start > MAX_CHUNK_SAMPLES:
                bounds.append((start, start + MAX_CHUNK_SAMPLES))
                start += MAX_CHUNK_SAMPLES
            if start < end:
                bounds.append((start, end))
    return bounds


def split_speech(audio: bytes) -> list[SpeechChunk]:
    with wave.open(io.BytesIO(audio)) as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise PermanentProviderError("stt: speech chunking requires 16 kHz mono PCM16 WAV")
        pcm = wav.readframes(wav.getnframes())
    if not pcm:
        return []
    chunks = []
    for a, b in chunk_bounds(speech_spans(pcm), len(pcm) // 2):
        out = io.BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm[a * 2 : b * 2])
        chunks.append(SpeechChunk(round(a / 16), round(b / 16), out.getvalue()))
    return chunks
