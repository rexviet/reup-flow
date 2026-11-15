"""Shared plumbing for the OpenAI-compatible HTTP providers."""

import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from localization.contracts.geometry import BBox
from localization.contracts.models import TranscriptDocument
from localization.contracts.voice import VoiceRef
from localization.observability.provider_spans import OUTCOME, STATUS, provider_span
from localization.providers.errors import (
    PermanentProviderError,
    TransientProviderError,
    classify_status,
)


@runtime_checkable
class SttProvider(Protocol):
    async def transcribe(
        self, audio: bytes, *, filename: str = ..., language: str | None = ...
    ) -> TranscriptDocument: ...


@runtime_checkable
class TtsProvider(Protocol):
    async def synthesize(self, text: str, voice: VoiceRef) -> bytes: ...


@runtime_checkable
class OcrProvider(Protocol):
    async def detect_region(self, frame_png: bytes, width: int, height: int) -> BBox | None: ...


@runtime_checkable
class StemSeparator(Protocol):
    def separate(
        self, input_path: Path, duration_ms: int = ..., cancel: threading.Event | None = ...
    ) -> tuple[Path, Path, Path]: ...


# The local voice server (VoiceStudio) is reached through Docker Desktop's host gateway, which
# drops about 40% of NEW connections to it (measured: 34 of 60 succeeded) while a connection that
# is already open keeps working. So a connection that does not open within a second is
# abandoned and opened again (httpx retries connection errors only, before any request is
# sent, so a speech or transcription request is never sent twice), and open connections stay
# in the pool for reuse. Measured with these settings: 60 of 60 succeeded, p95 2 ms.
LOCAL_CONNECT_TIMEOUT_SECONDS = 1.0
LOCAL_CONNECT_RETRIES = 5
LOCAL_KEEPALIVE_SECONDS = 120.0


def local_server_http() -> httpx.AsyncClient:
    """An HTTP client for a server on this machine: connection retries and a pool that
    keeps connections open between requests."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(
            retries=LOCAL_CONNECT_RETRIES,
            limits=httpx.Limits(keepalive_expiry=LOCAL_KEEPALIVE_SECONDS),
        )
    )


class OpenAICompatClient:
    """POSTs to `{base_url}{path}` with a bearer key and maps every failure to a
    transient or permanent provider error. Each request runs in a `provider.{name}`
    span that names `provider` and `model` and counts the request's `units` of input."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        *,
        timeout_seconds: float,
        http: httpx.AsyncClient | None = None,
        provider: str = "openai-compatible",
        model: str | None = None,
        connect_timeout: float | None = None,
    ) -> None:
        self._name = name
        self._provider = provider
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # `timeout_seconds` bounds reading the answer; `connect_timeout`, when given, bounds
        # opening the connection on its own, so a dropped connect is retried quickly.
        self._timeout = (
            httpx.Timeout(timeout_seconds, connect=connect_timeout)
            if connect_timeout is not None
            else timeout_seconds
        )
        self._http = http or httpx.AsyncClient()

    async def post(self, path: str, *, units: float | None = None, **kwargs: Any) -> httpx.Response:
        return await self._send("POST", path, units, **kwargs)

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._send("GET", path, None, **kwargs)

    async def _send(
        self, method: str, path: str, units: float | None, **kwargs: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        # Never a header, a body, the path or the base URL (which may carry a private
        # address) on the span: see `provider_spans`.
        with provider_span(
            self._name, provider=self._provider, model=self._model, input_units=units
        ) as span:
            try:
                response = await self._http.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=headers,
                    timeout=self._timeout,
                    **kwargs,
                )
            except httpx.TimeoutException:
                span.set_attribute(OUTCOME, "timeout")
                raise TransientProviderError(f"{self._name}: timeout") from None
            except httpx.TransportError:
                span.set_attribute(OUTCOME, "connection_error")
                raise TransientProviderError(f"{self._name}: connection error") from None
            span.set_attribute(STATUS, response.status_code)
            if not 200 <= response.status_code < 300:
                raise classify_status(self._name, response.status_code)
            return response

    def json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise PermanentProviderError(f"{self._name}: response is not JSON") from None
