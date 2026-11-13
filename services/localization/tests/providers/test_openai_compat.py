"""The real STT/TTS provider classes against a real local HTTP server."""

import pytest
from tests.support.stub_server import STT_BODY, WAV, Scenario, StubServer

from localization.config import Settings
from localization.contracts import VoiceRef, VoiceStudioSettings
from localization.providers.errors import (
    PermanentProviderError,
    TransientProviderError,
    to_application_error,
)
from localization.providers.openai_stt import OpenAICompatStt
from localization.providers.openai_tts import OpenAICompatTts

VOICE = VoiceRef(
    provider="voicestudio",
    voice_id="voice-7",
    settings=VoiceStudioSettings(speed=1.2, instructions="calm"),
)


def _settings(stub: StubServer, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("VOICESTUDIO_URL", stub.url)
    monkeypatch.setenv("VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS", "127.0.0.0/8")
    monkeypatch.setenv("VOICESTUDIO_API_KEY", "k-test")
    monkeypatch.setenv("STT_MODEL", "stt-model")
    monkeypatch.setenv("TTS_MODEL", "tts-model")
    monkeypatch.setenv("PROVIDER_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("TTS_TIMEOUT_SECONDS", "0.5")
    return Settings()


async def test_tts_sends_model_voice_and_returns_the_audio(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    tts = OpenAICompatTts.from_settings(_settings(stub, monkeypatch))
    audio = await tts.synthesize("xin chao", VOICE)
    assert audio == WAV
    request = stub.requests[0]
    assert request.path == "/v1/audio/speech"
    assert request.headers["authorization"] == "Bearer k-test"
    import json

    body = json.loads(request.body)
    assert body["model"] == "tts-model"
    assert body["voice"] == "voice-7"
    assert body["input"] == "xin chao"
    assert body["speed"] == 1.2
    assert body["instructions"] == "calm"


async def test_stt_requests_word_and_segment_timestamps_and_parses_them(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    stt = OpenAICompatStt.from_settings(_settings(stub, monkeypatch))
    doc = await stt.transcribe(b"RIFFxxxx", language="zh")
    request = stub.requests[0]
    assert request.path == "/v1/audio/transcriptions"
    for needle in (b"verbose_json", b"word", b"segment", b"stt-model", b'name="language"'):
        assert needle in request.body, needle
    assert request.body.count(b'name="timestamp_granularities[]"') == 2
    assert doc.language == "zh"
    assert doc.duration_ms == 3000
    assert [(s.start_ms, s.end_ms, s.text) for s in doc.segments] == [(0, 1500, "ni hao")]
    assert [(w.text, w.start_ms, w.end_ms) for w in doc.words] == [
        ("ni", 0, 700),
        ("hao", 700, 1500),
    ]


@pytest.mark.parametrize(
    "body",
    [{"text": "hi"}, {"segments": None}, {"segments": [{"start": 0}]}, ["not", "an", "object"]],
)
def test_an_stt_answer_that_cannot_be_read_is_retryable_and_names_only_its_shape(body) -> None:
    with pytest.raises(TransientProviderError, match="unexpected response shape") as raised:
        OpenAICompatStt._parse(body)
    # What the answer said never reaches the message, history or a log line.
    assert "hi" not in str(raised.value).replace("shape", "")


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
async def test_transient_statuses_are_retryable(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    stub.scenario = Scenario(status=status)
    tts = OpenAICompatTts.from_settings(_settings(stub, monkeypatch))
    with pytest.raises(TransientProviderError) as info:
        await tts.synthesize("x", VOICE)
    assert to_application_error(info.value).non_retryable is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_other_4xx_are_permanent_and_non_retryable(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    stub.scenario = Scenario(status=status)
    tts = OpenAICompatTts.from_settings(_settings(stub, monkeypatch))
    with pytest.raises(PermanentProviderError) as info:
        await tts.synthesize("x", VOICE)
    assert to_application_error(info.value).non_retryable is True


async def test_timeout_is_transient(stub: StubServer, monkeypatch: pytest.MonkeyPatch) -> None:
    stub.scenario = Scenario(delay_seconds=1.5)
    tts = OpenAICompatTts.from_settings(_settings(stub, monkeypatch))
    with pytest.raises(TransientProviderError, match="timeout"):
        await tts.synthesize("x", VOICE)


async def test_tts_can_wait_for_audio_longer_than_stt_without_retrying(stub, monkeypatch):
    settings = _settings(stub, monkeypatch).model_copy(
        update={
            "provider_timeout_seconds": 0.05,
            "tts_timeout_seconds": 0.8,
        }
    )
    stub.scenario = Scenario(delay_seconds=0.2, raw_body=WAV)
    assert await OpenAICompatTts.from_settings(settings).synthesize("xin chào", VOICE) == WAV
    assert len(stub.requests) == 1
    stub.scenario = Scenario(delay_seconds=0.2, json_body=STT_BODY)
    with pytest.raises(TransientProviderError, match="timeout"):
        await OpenAICompatStt.from_settings(settings).transcribe(b"audio")


async def test_connection_failure_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    dead = StubServer().start()
    settings = _settings(dead, monkeypatch)
    dead.stop()
    with pytest.raises(TransientProviderError):
        await OpenAICompatTts.from_settings(settings).synthesize("x", VOICE)


@pytest.mark.parametrize("status", [301, 302, 307])
async def test_a_redirect_is_not_a_success(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    stub.scenario = Scenario(status=status)
    tts = OpenAICompatTts.from_settings(_settings(stub, monkeypatch))
    with pytest.raises(PermanentProviderError):
        await tts.synthesize("x", VOICE)


@pytest.mark.parametrize("status", [301, 302, 307])
async def test_a_redirect_is_not_a_success_for_stt(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # The redirect carries a perfectly valid transcript, so only the status check
    # can refuse it.
    stub.scenario = Scenario(status=status, body_with_status=STT_BODY)
    stt = OpenAICompatStt.from_settings(_settings(stub, monkeypatch))
    with pytest.raises(PermanentProviderError):
        await stt.transcribe(b"x")


@pytest.mark.parametrize("body", [{"error": {"message": "quota"}}, {"text": "x"}, []])
async def test_a_200_without_segments_is_not_a_transcript(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    stub.scenario = Scenario(json_body=body)
    stt = OpenAICompatStt.from_settings(_settings(stub, monkeypatch))
    with pytest.raises(PermanentProviderError):
        await stt.transcribe(b"x")


@pytest.mark.parametrize(
    "body",
    [
        b'{"error": {"message": "quota exceeded"}}',
        b"<html>gateway</html>",
        b"RIFF",  # truncated header
        b"RIFF\x00\x00\x00\x00AVI LIST",  # a RIFF container that is not WAVE
    ],
)
async def test_a_200_that_is_not_a_wav_is_never_returned_as_audio(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    stub.scenario = Scenario(raw_body=body)
    tts = OpenAICompatTts.from_settings(_settings(stub, monkeypatch))
    with pytest.raises(PermanentProviderError, match="not a WAV"):
        await tts.synthesize("x", VOICE)


async def test_empty_audio_is_permanent(stub: StubServer, monkeypatch: pytest.MonkeyPatch) -> None:
    stub.scenario = Scenario(raw_body=b"")
    tts = OpenAICompatTts.from_settings(_settings(stub, monkeypatch))
    with pytest.raises(PermanentProviderError):
        await tts.synthesize("x", VOICE)


async def test_malformed_stt_body_is_permanent(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.scenario = Scenario(json_body={"segments": [{"nope": 1}]})
    stt = OpenAICompatStt.from_settings(_settings(stub, monkeypatch))
    with pytest.raises(PermanentProviderError):
        await stt.transcribe(b"x")


def test_missing_configuration_fails_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PermanentProviderError):
        OpenAICompatTts.from_settings(Settings())
    with pytest.raises(PermanentProviderError):
        OpenAICompatStt.from_settings(Settings())


def _answer(segments=None, words=None) -> dict:
    body: dict = {"segments": segments if segments is not None else [], "language": "ko"}
    if words is not None:
        body["words"] = words
    return body


SEG = {"id": 0, "start": 0.0, "end": 1.0, "text": "SECRETTEXT"}


@pytest.mark.parametrize(
    ("body", "names"),
    [
        (
            _answer([SEG, {**SEG, "id": 1, "start": 5.0, "end": 4.0}]),
            ["segments[1]", "fails its check", "start 5.0, end 4.0"],
        ),
        (_answer([SEG, {"id": 1, "start": 1.0}]), ["segments[1] has no 'end'"]),
        (
            _answer([{**SEG, "end": "soon"}]),
            ["segments[0].end is not a usable number (str)"],
        ),
        (
            _answer([{**SEG, "start": float("nan")}]),
            ["segments[0].start is not a usable number (nan)"],
        ),
        (_answer([SEG, "oops"]), ["segments[1] is a str, not an object"]),
        (
            _answer([SEG], words=[{"word": "a", "start": 0.0, "end": 0.5}, {"start": 0, "end": 1}]),
            ["words[1] has no 'word'"],
        ),
        (
            _answer([SEG], words=[{"word": "x" * 201, "start": 0.0, "end": 0.5}]),
            ["words[0].text fails its check", "string_too_long"],
        ),
        (
            _answer([SEG], words=[{"word": "SECRETTEXT", "start": 2.0, "end": 1.0}]),
            ["words[0]", "fails its check", "start 2.0, end 1.0"],
        ),
        (
            _answer([{**SEG, "words": [{"word": "a", "start": 0.0, "end": "x"}]}]),
            ["words[0].end is not a usable number (str)"],
        ),
    ],
)
def test_an_unreadable_stt_item_is_named_by_place_and_field_but_never_by_its_text(
    body, names
) -> None:
    with pytest.raises(TransientProviderError) as raised:
        OpenAICompatStt._parse(body)
    message = str(raised.value)
    for name in names:
        assert name in message, message
    assert "SECRETTEXT" not in message
    assert "object with keys: language,segments" in message
