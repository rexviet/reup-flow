"""Vbee STT and TTS against an in-process fake of Vbee's HTTP API.

The fake answers with the shapes the real API gave in live calls: an STT job is
`{"transcriptId", "status": "PENDING"}` on submit and, when done, `COMPLETED` with
`utterances[]` of `{text, startTime, endTime}` in seconds; a TTS batch request is
`{"requestId", "status": "PROCESSING"}`, then `COMPLETED` with an `audioLink` that
redirects to the audio. Errors are `{"error": {"code", "message"}}`.
"""

import io
import json
import wave
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.support.spans import exported_spans

from localization.config import Settings
from localization.contracts.vbee_flow import SpeechTranscript, Utterance
from localization.contracts.voice import VbeeSettings, VoiceRef
from localization.providers.base import TtsProvider
from localization.providers.errors import PermanentProviderError, TransientProviderError
from localization.providers.vbee import VbeeClient, VbeeStt, VbeeTts

BASE = "https://api.vbee.test"
CDN = "https://cdn.vbee.test"
TOKEN = "vbee-token-for-tests"
APP_ID = "vbee-app-for-tests"
VOICE_ID = "vbee-voice-for-tests"


def wav(seconds: float, rate: int = 16000, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * channels * int(seconds * rate))
    return buffer.getvalue()


TTS_AUDIO = wav(0.5, rate=24000)

STT_DONE = {
    "transcriptId": "0de0ed3d-0000-0000-0000-000000000000",
    "status": "COMPLETED",
    "transcript": "ba mươi tuổi mà trông như hai mươi. bổ sung dinh dưỡng",
    "utterances": [
        {"text": "ba mươi tuổi mà trông như hai mươi.", "startTime": 0, "endTime": 4.5},
        {"text": "bổ sung dinh dưỡng", "startTime": 4.5, "endTime": 8.93},
    ],
    "audioDurationSeconds": 10,
}


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


@dataclass
class FakeVbee:
    """Routes like Vbee. Each poll answers the next status in its script, the last
    one repeating. `override` answers a (method, path) pair instead of the route."""

    stt_polls: list[dict] = field(default_factory=lambda: [{"status": "PENDING"}, STT_DONE])
    tts_polls: list[dict] = field(
        default_factory=lambda: [
            {"status": "PROCESSING"},
            {"status": "COMPLETED", "audioLink": f"{BASE}/v1/audio/links/r1"},
        ]
    )
    download: Callable[[httpx.Request], httpx.Response] | None = None
    override: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = field(
        default_factory=dict
    )
    requests: list[httpx.Request] = field(default_factory=list)

    def _next(self, script: list[dict]) -> dict:
        return script.pop(0) if len(script) > 1 else script[0]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if request.url.host == "cdn.vbee.test":
            if self.download is not None:
                return self.download(request)
            return httpx.Response(200, content=TTS_AUDIO, headers={"content-type": "audio/wav"})
        if key in self.override:
            return self.override[key](request)
        if key == ("POST", "/v1/stt"):
            return httpx.Response(200, json={"transcriptId": "t1", "status": "PENDING"})
        if key == ("GET", "/v1/stt/transcripts/t1"):
            return httpx.Response(200, json={"transcriptId": "t1", **self._next(self.stt_polls)})
        if key == ("POST", "/v1/tts"):
            return httpx.Response(200, json={"requestId": "r1", "status": "PROCESSING"})
        if key == ("GET", "/v1/tts/requests/r1"):
            return httpx.Response(200, json={"requestId": "r1", **self._next(self.tts_polls)})
        if key == ("GET", "/v1/audio/links/r1"):
            # The audio link is a redirect to the file's real home.
            return httpx.Response(302, headers={"location": f"{CDN}/audio/r1.wav?sig=abc"})
        return httpx.Response(404, json=error_body("NOT_FOUND", "no route"))

    def to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]

    def polls(self, path_prefix: str) -> int:
        return sum(1 for r in self.requests if r.url.path.startswith(path_prefix))


def client(fake: FakeVbee, *, timeout: float = 5.0) -> VbeeClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return VbeeClient(BASE, APP_ID, TOKEN, timeout_seconds=timeout, http=http)


def stt(fake: FakeVbee, *, poll_timeout: float = 5.0) -> VbeeStt:
    return VbeeStt(client(fake), poll_interval_seconds=0.0, poll_timeout_seconds=poll_timeout)


def tts(fake: FakeVbee, *, poll_timeout: float = 5.0) -> VbeeTts:
    return VbeeTts(client(fake), poll_interval_seconds=0.0, poll_timeout_seconds=poll_timeout)


def voice(speed: float = 1.0) -> VoiceRef:
    return VoiceRef(provider="vbee", voice_id=VOICE_ID, settings=VbeeSettings(speed=speed))


def respond(status: int, body: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=body)


def fail_with(exc: type[httpx.TransportError]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("boom", request=request)

    return handler


@pytest.fixture
def spans():
    with exported_spans() as exporter:
        yield exporter


def span_attrs(spans: InMemorySpanExporter, name: str) -> list[dict]:
    return [dict(s.attributes or {}) for s in spans.get_finished_spans() if s.name == name]


# --- STT ---------------------------------------------------------------------------------


async def test_stt_job_pending_then_completed_maps_utterances_to_ms(
    spans: InMemorySpanExporter,
) -> None:
    fake = FakeVbee()

    result = await stt(fake).transcribe(wav(9.0))

    assert result == SpeechTranscript(
        utterances=(
            Utterance(text="ba mươi tuổi mà trông như hai mươi.", start_ms=0, end_ms=4500),
            Utterance(text="bổ sung dinh dưỡng", start_ms=4500, end_ms=8930),
        ),
        audio_ms=9000,
    )
    submit = fake.requests[0]
    assert (submit.method, submit.url.path) == ("POST", "/v1/stt")
    assert submit.headers["authorization"] == f"Bearer {TOKEN}"
    assert submit.headers["app-id"] == APP_ID
    body = submit.content
    assert b'name="mode"\r\n\r\nasync' in body
    assert b'name="audioContent"' in body
    assert fake.polls("/v1/stt/transcripts/t1") == 2
    (attrs,) = span_attrs(spans, "provider.stt")
    assert attrs["provider"] == "vbee"
    assert attrs["input_units"] == 9.0
    assert attrs["http.status_code"] == 200


async def test_stt_sends_a_16k_mono_wav_as_it_is() -> None:
    fake = FakeVbee()
    audio = wav(2.0)

    await stt(fake).transcribe(audio)

    assert audio in fake.requests[0].content


async def test_stt_converts_stereo_or_odd_rate_audio_to_16k_mono() -> None:
    fake = FakeVbee()

    result = await stt(fake).transcribe(wav(2.0, rate=44100, channels=2))

    sent = fake.requests[0].content
    start = sent.index(b"RIFF")
    with wave.open(io.BytesIO(sent[start:])) as reader:
        assert (reader.getnchannels(), reader.getframerate()) == (1, 16000)
    assert result.audio_ms == 2000


async def test_stt_rejects_bytes_that_are_not_audio() -> None:
    fake = FakeVbee()

    with pytest.raises(PermanentProviderError):
        await stt(fake).transcribe(b"not a wav at all")

    assert fake.requests == []


async def test_stt_job_that_fails_is_permanent(spans: InMemorySpanExporter) -> None:
    fake = FakeVbee(
        stt_polls=[{"status": "PENDING"}, {"status": "FAILED", **error_body("STT_ERR", "x")}]
    )

    with pytest.raises(PermanentProviderError, match="FAILED"):
        await stt(fake).transcribe(wav(1.0))

    (attrs,) = span_attrs(spans, "provider.stt")
    assert attrs["provider.outcome"] == "job_failed"


async def test_stt_poll_timeout_is_transient(spans: InMemorySpanExporter) -> None:
    fake = FakeVbee(stt_polls=[{"status": "PENDING"}])
    provider = VbeeStt(client(fake), poll_interval_seconds=0.01, poll_timeout_seconds=0.05)

    with pytest.raises(TransientProviderError, match="not done"):
        await provider.transcribe(wav(1.0))

    assert fake.polls("/v1/stt/transcripts/t1") >= 1
    (attrs,) = span_attrs(spans, "provider.stt")
    assert attrs["provider.outcome"] == "poll_timeout"


async def test_stt_poll_rides_out_a_transient_error_and_keeps_the_paid_job() -> None:
    answers = [
        httpx.Response(503, json=error_body("INTERNAL_SERVER_ERROR", "x")),
        httpx.Response(200, json=STT_DONE),
    ]
    fake = FakeVbee(override={("GET", "/v1/stt/transcripts/t1"): lambda _r: answers.pop(0)})

    result = await stt(fake).transcribe(wav(9.0))

    assert len(result.utterances) == 2
    assert fake.polls("/v1/stt") == 3  # one submit, one failed poll, one good poll


async def test_stt_completed_with_a_broken_utterance_is_permanent() -> None:
    broken = {**STT_DONE, "utterances": [{"text": "x", "startTime": 5, "endTime": 1}]}
    fake = FakeVbee(stt_polls=[broken])

    with pytest.raises(PermanentProviderError, match="unexpected"):
        await stt(fake).transcribe(wav(1.0))


async def test_stt_completed_without_speech_is_an_empty_transcript() -> None:
    fake = FakeVbee(stt_polls=[{**STT_DONE, "transcript": "", "utterances": []}])

    result = await stt(fake).transcribe(wav(1.0))

    assert result == SpeechTranscript(utterances=(), audio_ms=1000)


async def test_stt_submit_without_a_transcript_id_is_permanent() -> None:
    fake = FakeVbee(override={("POST", "/v1/stt"): respond(200, {"status": "PENDING"})})

    with pytest.raises(PermanentProviderError):
        await stt(fake).transcribe(wav(1.0))


# --- TTS ---------------------------------------------------------------------------------


def test_tts_satisfies_the_tts_provider_protocol() -> None:
    assert isinstance(tts(FakeVbee()), TtsProvider)


async def test_tts_batch_flow_follows_the_redirect_to_the_audio(
    spans: InMemorySpanExporter,
) -> None:
    fake = FakeVbee()

    audio = await tts(fake).synthesize("Xin chào các bạn.", voice(speed=1.1))

    assert audio == TTS_AUDIO
    submit = fake.requests[0]
    assert (submit.method, submit.url.path) == ("POST", "/v1/tts")
    assert submit.headers["authorization"] == f"Bearer {TOKEN}"
    assert submit.headers["app-id"] == APP_ID
    body = json.loads(submit.content)
    assert body["text"] == "Xin chào các bạn."
    assert body["voiceCode"] == VOICE_ID
    assert body["mode"] == "async"
    assert body["outputFormat"] == "wav"
    assert body["speed"] == 1.1
    assert body["webhookUrl"].startswith("https://")
    assert fake.polls("/v1/tts/requests/r1") == 2
    (attrs,) = span_attrs(spans, "provider.tts")
    assert attrs["provider"] == "vbee"
    assert attrs["input_units"] == len("Xin chào các bạn.")


async def test_tts_download_carries_no_credential() -> None:
    fake = FakeVbee()

    await tts(fake).synthesize("Xin chào.", voice())

    downloads = [r for r in fake.requests if "/v1/audio/links/" in r.url.path] + fake.to(
        "cdn.vbee.test"
    )
    assert len(downloads) == 2
    for request in downloads:
        assert "authorization" not in request.headers
        assert "app-id" not in request.headers


@pytest.mark.parametrize("status", [403, 404])
async def test_tts_expired_audio_link_is_transient(
    status: int, spans: InMemorySpanExporter
) -> None:
    fake = FakeVbee(download=respond(status, {"message": "Request has expired"}))

    with pytest.raises(TransientProviderError, match="expired"):
        await tts(fake).synthesize("Xin chào.", voice())

    (attrs,) = span_attrs(spans, "provider.tts")
    assert attrs["provider.outcome"] == "link_expired"


async def test_tts_audio_that_is_not_a_wav_is_permanent() -> None:
    fake = FakeVbee(download=respond(200, {"oops": True}))

    with pytest.raises(PermanentProviderError, match="WAV"):
        await tts(fake).synthesize("Xin chào.", voice())


async def test_tts_request_that_fails_is_permanent() -> None:
    fake = FakeVbee(tts_polls=[{"status": "FAILED", **error_body("BAD_REQUEST", "x")}])

    with pytest.raises(PermanentProviderError, match="FAILED"):
        await tts(fake).synthesize("Xin chào.", voice())


async def test_tts_poll_timeout_is_transient() -> None:
    fake = FakeVbee(tts_polls=[{"status": "PROCESSING"}])
    provider = VbeeTts(client(fake), poll_interval_seconds=0.01, poll_timeout_seconds=0.05)

    with pytest.raises(TransientProviderError, match="not done"):
        await provider.synthesize("Xin chào.", voice())


async def test_tts_completed_without_an_audio_link_is_permanent() -> None:
    fake = FakeVbee(tts_polls=[{"status": "COMPLETED"}])

    with pytest.raises(PermanentProviderError, match="audio link"):
        await tts(fake).synthesize("Xin chào.", voice())


async def test_tts_refuses_a_voice_of_another_provider() -> None:
    fake = FakeVbee()
    other = VoiceRef.model_validate(
        {"provider": "voicestudio", "voice_id": "v", "settings": {"speed": 1.0}}
    )

    with pytest.raises(PermanentProviderError, match="not a vbee voice"):
        await tts(fake).synthesize("Xin chào.", other)

    assert fake.requests == []


# --- error mapping, shared by both ---------------------------------------------------------


SUBMITS = {"stt": ("POST", "/v1/stt"), "tts": ("POST", "/v1/tts")}


async def _submit(kind: str, fake: FakeVbee) -> None:
    if kind == "stt":
        await stt(fake).transcribe(wav(1.0))
    else:
        await tts(fake).synthesize("Xin chào.", voice())


@pytest.mark.parametrize("kind", ["stt", "tts"])
@pytest.mark.parametrize(
    "status, code",
    [
        (429, "TTS_CCR_MAX_LIMIT_REACHED"),
        (500, "INTERNAL_SERVER_ERROR"),
        (502, "BAD_GATEWAY"),
        (503, "SERVICE_UNAVAILABLE"),
    ],
)
async def test_rate_limit_and_server_errors_are_transient(
    kind: str, status: int, code: str
) -> None:
    fake = FakeVbee(override={SUBMITS[kind]: respond(status, error_body(code, "x"))})

    with pytest.raises(TransientProviderError, match=f"HTTP {status}"):
        await _submit(kind, fake)


@pytest.mark.parametrize("kind", ["stt", "tts"])
@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
async def test_network_errors_are_transient(kind: str, exc: type[httpx.TransportError]) -> None:
    fake = FakeVbee(override={SUBMITS[kind]: fail_with(exc)})

    with pytest.raises(TransientProviderError):
        await _submit(kind, fake)


@pytest.mark.parametrize("kind", ["stt", "tts"])
@pytest.mark.parametrize(
    "status, body",
    [
        (400, error_body("BAD_REQUEST", "voiceCode is invalid")),
        (401, error_body("UNAUTHORIZED", "Unauthorized")),
        (403, error_body("FORBIDDEN", "Missing feature: stt-sync")),
    ],
)
async def test_bad_request_auth_and_missing_feature_are_permanent(
    kind: str, status: int, body: dict
) -> None:
    fake = FakeVbee(override={SUBMITS[kind]: respond(status, body)})

    with pytest.raises(PermanentProviderError, match=f"HTTP {status}"):
        await _submit(kind, fake)


async def test_missing_feature_names_the_feature_and_nothing_else() -> None:
    fake = FakeVbee(
        override={
            SUBMITS["stt"]: respond(403, error_body("FORBIDDEN", "Missing feature: stt-sync"))
        }
    )

    with pytest.raises(PermanentProviderError) as raised:
        await _submit("stt", fake)

    assert str(raised.value) == "vbee stt: HTTP 403 FORBIDDEN (missing feature stt-sync)"


async def test_an_error_body_is_never_copied_into_the_error() -> None:
    echo = f"Bearer {TOKEN} App-Id {APP_ID} voice {VOICE_ID}"
    fake = FakeVbee(override={SUBMITS["tts"]: respond(401, error_body("UNAUTHORIZED", echo))})

    with pytest.raises(PermanentProviderError) as raised:
        await _submit("tts", fake)

    text = str(raised.value)
    for secret in (TOKEN, APP_ID, VOICE_ID):
        assert secret not in text
    assert text == "vbee tts: HTTP 401 UNAUTHORIZED"


async def test_an_error_code_that_is_not_a_plain_code_is_dropped() -> None:
    fake = FakeVbee(override={SUBMITS["tts"]: respond(401, error_body(f"Bearer {TOKEN}", "x"))})

    with pytest.raises(PermanentProviderError) as raised:
        await _submit("tts", fake)

    assert str(raised.value) == "vbee tts: HTTP 401"


# --- settings ------------------------------------------------------------------------------


def test_settings_read_vbee_env_as_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VBEE_APP_ID", APP_ID)
    monkeypatch.setenv("VBEE_API_TOKEN", TOKEN)
    monkeypatch.setenv("VBEE_VOICE_ID", VOICE_ID)

    settings = Settings()

    assert settings.vbee_base_url == "https://api.vbee.vn"
    assert settings.vbee_api_token is not None
    assert settings.vbee_api_token.get_secret_value() == TOKEN
    assert settings.vbee_app_id is not None
    assert settings.vbee_app_id.get_secret_value() == APP_ID
    assert settings.vbee_voice_id is not None
    assert settings.vbee_voice_id.get_secret_value() == VOICE_ID
    shown = repr(settings) + str(settings.model_dump())
    for secret in (TOKEN, APP_ID, VOICE_ID):
        assert secret not in shown


def test_settings_refuse_a_plain_http_vbee_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VBEE_BASE_URL", "http://api.vbee.vn")

    with pytest.raises(ValueError, match="VBEE_BASE_URL"):
        Settings()


@pytest.mark.parametrize("missing", ["VBEE_APP_ID", "VBEE_API_TOKEN"])
def test_providers_refuse_to_build_without_credentials(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in (("VBEE_APP_ID", APP_ID), ("VBEE_API_TOKEN", TOKEN)):
        if name != missing:
            monkeypatch.setenv(name, value)

    settings = Settings()

    for cls in (VbeeStt, VbeeTts):
        with pytest.raises(PermanentProviderError, match=missing):
            cls.from_settings(settings)


def test_providers_build_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VBEE_APP_ID", APP_ID)
    monkeypatch.setenv("VBEE_API_TOKEN", TOKEN)
    monkeypatch.setenv("VBEE_POLL_TIMEOUT_SECONDS", "42")

    settings = Settings()

    assert settings.vbee_poll_timeout_seconds == 42.0
    assert isinstance(VbeeStt.from_settings(settings), VbeeStt)
    assert isinstance(VbeeTts.from_settings(settings), VbeeTts)


def test_vbee_voice_settings_bound_speed_to_what_vbee_accepts() -> None:
    assert VbeeSettings().speed == 1.0
    for bad in (0.2, 2.0):
        with pytest.raises(ValueError):
            VbeeSettings(speed=bad)
    ref = VoiceRef.model_validate({"provider": "vbee", "voice_id": "v", "settings": {}})
    assert isinstance(ref.settings, VbeeSettings)
