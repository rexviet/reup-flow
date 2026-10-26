"""Shared plumbing for the OpenAI-compatible HTTP providers."""

from typing import Any

import httpx
from opentelemetry import trace

from localization.providers.errors import (
    PermanentProviderError,
    TransientProviderError,
    classify_status,
)

_tracer = trace.get_tracer("localization.providers")


class OpenAICompatClient:
    """POSTs to `{base_url}{path}` with a bearer key and maps every failure to a
    transient or permanent provider error."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        *,
        timeout_seconds: float,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._http = http or httpx.AsyncClient()

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        # Only the provider name and the path go on the span. Never a header, a
        # body or the base URL (which may carry a private address).
        with _tracer.start_as_current_span(
            f"provider.{self._name}", attributes={"provider.name": self._name, "http.path": path}
        ) as span:
            try:
                response = await self._http.post(
                    f"{self._base_url}{path}",
                    headers=headers,
                    timeout=self._timeout,
                    **kwargs,
                )
            except httpx.TimeoutException:
                span.set_attribute("provider.outcome", "timeout")
                raise TransientProviderError(f"{self._name}: timeout") from None
            except httpx.TransportError:
                span.set_attribute("provider.outcome", "connection_error")
                raise TransientProviderError(f"{self._name}: connection error") from None
            span.set_attribute("http.status_code", response.status_code)
            if response.status_code >= 400 or response.status_code < 200:
                raise classify_status(self._name, response.status_code)
            return response

    def json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise PermanentProviderError(f"{self._name}: response is not JSON") from None
