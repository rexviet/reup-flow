"""The span every call to an outside provider (STT, TTS, OCR, the voice list) runs in.

Named `provider.{kind}` and carrying only what is safe to export and enough to read a
cost from: which provider, which model, and how much input it was given
(`input_units`, whose unit depends on the kind, see `INPUT_UNITS`). Never the text, the
audio, the image, a header, a key, or a URL (a base URL may hold a private address).
The secret canary test exports these spans and searches them.
"""

import io
import wave
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Span

_tracer = trace.get_tracer("localization.providers")

PROVIDER = "provider"
MODEL = "model"
UNITS = "input_units"
OUTCOME = "provider.outcome"
STATUS = "http.status_code"

# What `input_units` counts for each kind of call.
INPUT_UNITS = {
    "stt": "seconds of audio",
    "tts": "characters of text",
    "ocr": "images",
}


def span_name(kind: str) -> str:
    return f"provider.{kind}"


@contextmanager
def provider_span(
    kind: str, *, provider: str, model: str | None, input_units: float | None
) -> Iterator[Span]:
    attributes: dict[str, str | float] = {PROVIDER: provider}
    if model:
        attributes[MODEL] = model
    if input_units is not None:
        attributes[UNITS] = input_units
    with _tracer.start_as_current_span(span_name(kind), attributes=attributes) as span:
        yield span


def wav_seconds(audio: bytes) -> float | None:
    """The length of a WAV file, or None when the bytes are not one that says."""
    try:
        with wave.open(io.BytesIO(audio)) as reader:
            rate = reader.getframerate()
            return round(reader.getnframes() / rate, 3) if rate else None
    except (wave.Error, EOFError, ValueError):
        return None
