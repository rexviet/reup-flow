"""Live smoke test of the production Vbee providers: one TTS line and one STT clip.

Run: uv run python eval/vbee_provider_smoke.py STT_CLIP.wav [--text "..."]

Makes two paid calls, one batch TTS request and one STT job, plus their polls, through
`VbeeTts` and `VbeeStt` exactly as a worker builds them from Settings. VBEE_API_TOKEN,
VBEE_APP_ID and VBEE_VOICE_ID come from the repo .env and are never printed; neither is
the audio. Prints each call's latency, the shape of the TTS audio and the transcript.
"""

import argparse
import asyncio
import io
import sys
import time
import wave
from pathlib import Path

import httpx
from common import load_env

from localization.config import Settings
from localization.contracts.voice import VbeeSettings, VoiceRef
from localization.providers.errors import ProviderError
from localization.providers.vbee import VbeeStt, VbeeTts

DEFAULT_TEXT = "Xin chào các bạn, hôm nay mình thử giọng đọc mới."


def _wav_shape(audio: bytes) -> str:
    with wave.open(io.BytesIO(audio)) as reader:
        seconds = reader.getnframes() / reader.getframerate()
        return (
            f"{reader.getframerate()} Hz, {reader.getnchannels()} ch, "
            f"{8 * reader.getsampwidth()}-bit, {seconds:.2f} s"
        )


async def run(clip: Path, text: str) -> int:
    settings = Settings()
    if settings.vbee_voice_id is None:
        print("VBEE_VOICE_ID is not set", file=sys.stderr)
        return 2
    voice = VoiceRef(
        provider="vbee",
        voice_id=settings.vbee_voice_id.get_secret_value(),
        settings=VbeeSettings(),
    )
    failed = False
    async with httpx.AsyncClient() as http:
        tts = VbeeTts.from_settings(settings, http)
        started = time.monotonic()
        try:
            audio = await tts.synthesize(text, voice)
            print(f"tts: ok in {time.monotonic() - started:.2f} s, {len(text)} chars")
            print(f"tts: {_wav_shape(audio)}, {len(audio)} bytes")
        except ProviderError as exc:
            failed = True
            print(f"tts: {type(exc).__name__}: {exc} after {time.monotonic() - started:.2f} s")

        stt = VbeeStt.from_settings(settings, http)
        started = time.monotonic()
        try:
            transcript = await stt.transcribe(clip.read_bytes())
            print(f"stt: ok in {time.monotonic() - started:.2f} s, audio {transcript.audio_ms} ms")
            for u in transcript.utterances:
                print(f"stt:   [{u.start_ms}-{u.end_ms} ms] {u.text}")
        except ProviderError as exc:
            failed = True
            print(f"stt: {type(exc).__name__}: {exc} after {time.monotonic() - started:.2f} s")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clip", type=Path, help="WAV clip for STT")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="the line for TTS")
    args = parser.parse_args()
    if not args.clip.is_file():
        print(f"no such clip: {args.clip}", file=sys.stderr)
        return 2
    load_env()
    return asyncio.run(run(args.clip, args.text))


if __name__ == "__main__":
    sys.exit(main())
