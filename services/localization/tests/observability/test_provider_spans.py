"""Each call to STT, TTS and OCR leaves a `provider.{kind}` span with the provider, the
model and how much input it was given, and nothing of the content, the address or the
key. The real provider classes talk to a local stub server."""

import io
import wave
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from localization.config import Settings
from localization.contracts.voice import VoiceRef
from localization.observability.provider_spans import wav_seconds
from localization.providers.openai_stt import OpenAICompatStt
from localization.providers.openai_tts import OpenAICompatTts
from localization.providers.vision_ocr import VisionOcr
from tests.support.spans import exported_spans
from tests.support.stub_server import Scenario, StubServer

KEY = "sk-provider-span-canary"
SPOKEN = "xin chào các bạn, hôm nay"
ALLOWED = {"provider", "model", "input_units", "http.status_code", "provider.outcome"}


def _wav(seconds: float, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


@pytest.fixture
def settings(stub: StubServer, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("VOICESTUDIO_URL", stub.url)
    monkeypatch.setenv("VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS", "127.0.0.0/8")
    monkeypatch.setenv("VOICESTUDIO_API_KEY", KEY)
    monkeypatch.setenv("OPENROUTER_BASE_URL", stub.url)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("STT_MODEL", "whisperx")
    monkeypatch.setenv("TTS_MODEL", "omnivoice")
    monkeypatch.setenv("OCR_MODEL", "vision/model")
    monkeypatch.setenv("OCR_BBOX_FORMAT", "xyxy_px")
    return Settings()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    with exported_spans() as exporter:
        yield exporter


def _only(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    (span,) = [s for s in spans.get_finished_spans() if s.name == name]
    return span


def _no_content(span: ReadableSpan, stub: StubServer) -> None:
    attributes = dict(span.attributes or {})
    assert set(attributes) <= ALLOWED, attributes
    text = repr(attributes)
    for secret in (KEY, SPOKEN, stub.url, "127.0.0.1", "/v1/", "base64"):
        assert secret not in text


async def test_stt_counts_seconds_of_audio(
    settings: Settings, stub: StubServer, spans: InMemorySpanExporter
) -> None:
    await OpenAICompatStt.from_settings(settings).transcribe(_wav(2.5))
    span = _only(spans, "provider.stt")
    assert dict(span.attributes or {}) == {
        "provider": "voicestudio",
        "model": "whisperx",
        "input_units": 2.5,
        "http.status_code": 200,
    }
    _no_content(span, stub)


async def test_tts_counts_characters_of_text(
    settings: Settings, stub: StubServer, spans: InMemorySpanExporter
) -> None:
    voice = VoiceRef(provider="voicestudio", voice_id="voice-a", settings={})  # type: ignore[arg-type]
    await OpenAICompatTts.from_settings(settings).synthesize(SPOKEN, voice)
    span = _only(spans, "provider.tts")
    assert dict(span.attributes or {}) == {
        "provider": "voicestudio",
        "model": "omnivoice",
        "input_units": len(SPOKEN),
        "http.status_code": 200,
    }
    _no_content(span, stub)


async def test_ocr_counts_one_image(
    settings: Settings, stub: StubServer, spans: InMemorySpanExporter
) -> None:
    stub.scenario = Scenario(
        json_body={"choices": [{"message": {"content": '{"bbox": [100, 800, 900, 900]}'}}]}
    )
    await VisionOcr.from_settings(settings).detect_region(b"\x89PNG fake", 1080, 1920)
    span = _only(spans, "provider.ocr")
    assert dict(span.attributes or {}) == {
        "provider": "openrouter",
        "model": "vision/model",
        "input_units": 1,
        "http.status_code": 200,
    }
    _no_content(span, stub)


async def test_a_failed_call_still_names_provider_and_model(
    settings: Settings, stub: StubServer, spans: InMemorySpanExporter
) -> None:
    stub.scenario = Scenario(status=500, echo_authorization=True)
    with pytest.raises(Exception):  # noqa: B017 - which error is the client's business
        await OpenAICompatStt.from_settings(settings).transcribe(_wav(1.0))
    span = _only(spans, "provider.stt")
    assert span.attributes["model"] == "whisperx"
    assert span.attributes["http.status_code"] == 500
    _no_content(span, stub)


def test_audio_that_is_not_a_wav_has_no_length() -> None:
    assert wav_seconds(_wav(0.5)) == 0.5
    assert wav_seconds(b"RIFF\x24\x00\x00\x00WAVEfmt ") is None
    assert wav_seconds(b"") is None
