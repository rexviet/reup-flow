"""Vbee speech: STT as an async job, TTS as a batch request, both polled.

STT posts a WAV (`POST /v1/stt`, `mode=async`) and polls
`GET /v1/stt/transcripts/{id}`; what it hears comes back as utterances with a start
and an end in seconds. TTS posts the text (`POST /v1/tts`, `mode=async`), which
Vbee only accepts with a webhook, so it is given a placeholder that nothing listens
on, and polls `GET /v1/tts/requests/{id}` until the request has an audio link. That
link lives 3 minutes and redirects to the file, so it is fetched at once, without any
of our headers.

The token, the app id and the voice id never reach an error, a log line or a span.
Errors are built from the HTTP status and, when it is a plain upper-case code, the
`error.code` of Vbee's body: never the body's message, which can echo a request.
"""

import asyncio
import io
import re
import tempfile
import time
import wave
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from opentelemetry.trace import Span
from pydantic import ValidationError

from localization.activities.ffmpeg import to_stt_audio
from localization.config import Settings
from localization.contracts.vbee_flow import SpeechTranscript, Utterance
from localization.contracts.voice import VbeeSettings, VoiceRef
from localization.observability.provider_spans import OUTCOME, STATUS, provider_span
from localization.providers.errors import (
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
    classify_status,
)

PROVIDER = "vbee"
# Batch TTS refuses a request without a webhook. The result is polled instead; this
# address is reserved for examples and receives nothing of ours but the callback.
PLACEHOLDER_WEBHOOK = "https://example.com/vbee-tts-callback"
STT_POLL_SECONDS = 3.0
TTS_POLL_SECONDS = 1.0
# The sample rates Vbee's STT accepts, and the largest file it takes.
STT_SAMPLE_RATES = frozenset({8000, 16000, 22050, 24000, 32000, 44100, 48000})
STT_MAX_BYTES = 100 * 1024 * 1024
# A download that answers one of these has outlived its 3 minutes.
EXPIRED_LINK_STATUSES = frozenset({403, 404, 410})

_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_MISSING_FEATURE_RE = re.compile(r"Missing feature: ([a-z0-9][a-z0-9-]{0,63})")


def _error_detail(response: httpx.Response) -> str:
    """` CODE`, plus the missing feature's name for a plan that lacks one; empty when
    the body says neither in a form that cannot carry anything else."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(error, dict):
        return ""
    code, message = error.get("code"), error.get("message")
    if not isinstance(code, str) or _CODE_RE.fullmatch(code) is None:
        return ""
    feature = _MISSING_FEATURE_RE.fullmatch(message) if isinstance(message, str) else None
    return f" {code}" + (f" (missing feature {feature.group(1)})" if feature else "")


def _status_error(name: str, response: httpx.Response) -> ProviderError:
    error = classify_status(name, response.status_code)
    return type(error)(f"{error}{_error_detail(response)}")


def _wav_shape(audio: bytes) -> tuple[int, int, int, int] | None:
    """(channels, sample width, rate, frames) of a PCM WAV, or None."""
    try:
        with wave.open(io.BytesIO(audio)) as reader:
            return (
                reader.getnchannels(),
                reader.getsampwidth(),
                reader.getframerate(),
                reader.getnframes(),
            )
    except (wave.Error, EOFError, ValueError):
        return None


def _to_16k_mono(audio: bytes) -> bytes:
    """Blocking: ffmpeg rewrites the audio as 16 kHz mono PCM16."""
    with tempfile.TemporaryDirectory(prefix="vbee-stt-") as tmp:
        source, out = Path(tmp) / "in.audio", Path(tmp) / "out.wav"
        source.write_bytes(audio)
        to_stt_audio(source, out)
        return out.read_bytes()


async def stt_wav(audio: bytes) -> tuple[bytes, int]:
    """The audio as Vbee's STT takes it, and its length in ms. A mono PCM16 WAV at an
    accepted rate goes as it is; anything else is converted to 16 kHz mono."""
    shape = _wav_shape(audio)
    if shape is None or shape[:2] != (1, 2) or shape[2] not in STT_SAMPLE_RATES:
        if not audio.startswith(b"RIFF"):
            raise PermanentProviderError("vbee stt: the audio is not a WAV file")
        audio = await asyncio.to_thread(_to_16k_mono, audio)
        shape = _wav_shape(audio)
        if shape is None:
            raise PermanentProviderError("vbee stt: the audio could not be converted")
    if len(audio) > STT_MAX_BYTES:
        raise PermanentProviderError("vbee stt: the audio is over Vbee's 100 MB limit")
    _channels, _width, rate, frames = shape
    return audio, round(frames * 1000 / rate)


class VbeeClient:
    """Requests to Vbee's API with its two credentials, each failure mapped to a
    transient or permanent provider error: 408, 429, 5xx and the network are
    transient; any other 4xx (400, 401, 403 "Missing feature") is permanent."""

    def __init__(
        self,
        base_url: str,
        app_id: str,
        token: str,
        *,
        timeout_seconds: float,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "App-Id": app_id}
        self._timeout = timeout_seconds
        self._http = http or httpx.AsyncClient()

    @classmethod
    def from_settings(
        cls, settings: Settings, http: httpx.AsyncClient | None = None
    ) -> "VbeeClient":
        app_id = settings.vbee_app_id.get_secret_value() if settings.vbee_app_id else ""
        token = settings.vbee_api_token.get_secret_value() if settings.vbee_api_token else ""
        missing = [n for n, v in (("VBEE_APP_ID", app_id), ("VBEE_API_TOKEN", token)) if not v]
        if missing:
            raise PermanentProviderError(f"vbee: {' and '.join(missing)} must be set")
        return cls(
            settings.vbee_base_url,
            app_id,
            token,
            timeout_seconds=settings.provider_timeout_seconds,
            http=http,
        )

    async def call(self, name: str, span: Span, method: str, path: str, **kwargs: Any) -> Any:
        """The JSON body of a 2xx answer to `method path`."""
        response = await self._send(
            name, span, method, f"{self._base_url}{path}", self._headers, **kwargs
        )
        if not 200 <= response.status_code < 300:
            raise _status_error(name, response)
        try:
            return response.json()
        except ValueError:
            raise PermanentProviderError(f"{name}: response is not JSON") from None

    async def download(self, name: str, span: Span, url: str) -> bytes:
        """The file behind an audio link, following its redirects and sending none of
        our headers: the link is already signed, and it leads off Vbee's API."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise PermanentProviderError(f"{name}: the audio link is not an http(s) URL")
        response = await self._send(name, span, "GET", url, {}, follow_redirects=True)
        if response.status_code in EXPIRED_LINK_STATUSES:
            span.set_attribute(OUTCOME, "link_expired")
            raise TransientProviderError(
                f"{name}: the audio link expired (HTTP {response.status_code})"
            )
        if not 200 <= response.status_code < 300:
            raise classify_status(name, response.status_code)
        return response.content

    async def _send(
        self, name: str, span: Span, method: str, url: str, headers: dict[str, str], **kwargs: Any
    ) -> httpx.Response:
        try:
            response = await self._http.request(
                method, url, headers=headers, timeout=self._timeout, **kwargs
            )
        except httpx.TimeoutException:
            span.set_attribute(OUTCOME, "timeout")
            raise TransientProviderError(f"{name}: timeout") from None
        except httpx.TransportError:
            span.set_attribute(OUTCOME, "connection_error")
            raise TransientProviderError(f"{name}: connection error") from None
        except httpx.HTTPError:
            # A redirect loop or a malformed link: not something a retry fixes.
            span.set_attribute(OUTCOME, "http_error")
            raise PermanentProviderError(f"{name}: the request could not be made") from None
        span.set_attribute(STATUS, response.status_code)
        return response


async def _poll(
    client: VbeeClient,
    name: str,
    span: Span,
    path: str,
    *,
    interval: float,
    deadline: float,
) -> dict[str, Any]:
    """The job's body once it is COMPLETED. A transient error while polling is ridden
    out until the deadline, because giving up would pay for the job again."""
    last: TransientProviderError | None = None
    while True:
        if time.monotonic() >= deadline:
            span.set_attribute(OUTCOME, "poll_timeout")
            raise last or TransientProviderError(f"{name}: the job is not done in time")
        await asyncio.sleep(interval)
        try:
            body = await client.call(name, span, "GET", path)
        except TransientProviderError as exc:
            last = exc
            continue
        status = body.get("status") if isinstance(body, dict) else None
        if not isinstance(status, str):
            raise PermanentProviderError(f"{name}: unexpected poll response")
        if status == "COMPLETED":
            return body
        if status == "FAILED":
            span.set_attribute(OUTCOME, "job_failed")
            detail = ""
            error = body.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, str) and _CODE_RE.fullmatch(code):
                detail = f" {code}"
            raise PermanentProviderError(f"{name}: the job FAILED{detail}")


def _job_id(body: Any, key: str, name: str) -> str:
    value = body.get(key) if isinstance(body, dict) else None
    if not isinstance(value, str) or not value or "/" in value:
        raise PermanentProviderError(f"{name}: the submit answer has no {key}")
    return value


def _ms(seconds: Any) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        raise TypeError("a time must be a number")
    return max(0, round(float(seconds) * 1000))


class VbeeStt:
    """What was said in a vocals track, as Vbee's utterances. Vbee answers Chinese
    speech in Vietnamese."""

    _NAME = "vbee stt"

    def __init__(
        self,
        client: VbeeClient,
        *,
        poll_interval_seconds: float = STT_POLL_SECONDS,
        poll_timeout_seconds: float = 300.0,
    ) -> None:
        self._client = client
        self._interval = poll_interval_seconds
        self._timeout = poll_timeout_seconds

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        *,
        poll_interval_seconds: float = STT_POLL_SECONDS,
    ) -> "VbeeStt":
        return cls(
            VbeeClient.from_settings(settings, http),
            poll_interval_seconds=poll_interval_seconds,
            poll_timeout_seconds=settings.vbee_poll_timeout_seconds,
        )

    async def transcribe(self, wav: bytes) -> SpeechTranscript:
        audio, audio_ms = await stt_wav(wav)
        with provider_span(
            "stt", provider=PROVIDER, model=None, input_units=audio_ms / 1000
        ) as span:
            deadline = time.monotonic() + self._timeout
            submitted = await self._client.call(
                self._NAME,
                span,
                "POST",
                "/v1/stt",
                data={"mode": "async"},
                files={"audioContent": ("audio.wav", audio, "audio/wav")},
            )
            job = _job_id(submitted, "transcriptId", self._NAME)
            body = await _poll(
                self._client,
                self._NAME,
                span,
                f"/v1/stt/transcripts/{job}",
                interval=self._interval,
                deadline=deadline,
            )
        return self._parse(body, audio_ms)

    @classmethod
    def _parse(cls, body: dict[str, Any], audio_ms: int) -> SpeechTranscript:
        raw = body.get("utterances")
        if raw is None:
            raw = []
        try:
            if not isinstance(raw, list):
                raise TypeError("utterances must be a list")
            utterances = tuple(
                Utterance(
                    text=item["text"].strip(),
                    start_ms=_ms(item["startTime"]),
                    end_ms=_ms(item["endTime"]),
                )
                for item in raw
                if not isinstance(item.get("text"), str) or item["text"].strip()
            )
            return SpeechTranscript(utterances=utterances, audio_ms=audio_ms)
        except (KeyError, TypeError, AttributeError, ValidationError):
            raise PermanentProviderError(f"{cls._NAME}: unexpected transcript shape") from None


class VbeeTts:
    """A line of text as WAV audio in a Vbee voice, through the batch API: the only
    one that speaks a cloned voice. Satisfies `TtsProvider`."""

    _NAME = "vbee tts"

    def __init__(
        self,
        client: VbeeClient,
        *,
        poll_interval_seconds: float = TTS_POLL_SECONDS,
        poll_timeout_seconds: float = 300.0,
    ) -> None:
        self._client = client
        self._interval = poll_interval_seconds
        self._timeout = poll_timeout_seconds

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        *,
        poll_interval_seconds: float = TTS_POLL_SECONDS,
    ) -> "VbeeTts":
        return cls(
            VbeeClient.from_settings(settings, http),
            poll_interval_seconds=poll_interval_seconds,
            poll_timeout_seconds=settings.vbee_poll_timeout_seconds,
        )

    async def synthesize(self, text: str, voice: VoiceRef) -> bytes:
        if voice.provider != PROVIDER or not isinstance(voice.settings, VbeeSettings):
            raise PermanentProviderError(f"{self._NAME}: the voice is not a vbee voice")
        payload = {
            "text": text,
            "voiceCode": voice.voice_id,
            "mode": "async",
            "outputFormat": "wav",
            "speed": voice.settings.speed,
            "webhookUrl": PLACEHOLDER_WEBHOOK,
        }
        with provider_span("tts", provider=PROVIDER, model=None, input_units=len(text)) as span:
            deadline = time.monotonic() + self._timeout
            submitted = await self._client.call(self._NAME, span, "POST", "/v1/tts", json=payload)
            request = _job_id(submitted, "requestId", self._NAME)
            body = await _poll(
                self._client,
                self._NAME,
                span,
                f"/v1/tts/requests/{request}",
                interval=self._interval,
                deadline=deadline,
            )
            link = body.get("audioLink")
            if not isinstance(link, str) or not link:
                raise PermanentProviderError(f"{self._NAME}: COMPLETED without an audio link")
            audio = await self._client.download(self._NAME, span, link)
        # The clip is cached under a hash of its inputs, so anything that is not a WAV
        # must never be stored as if it were audio.
        if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise PermanentProviderError(f"{self._NAME}: the audio is not a WAV file")
        return audio
