"""The provider's voice list. A `voice_id` that matches no profile is silently
replaced by an engine preset on the server, so an id that is not in this list is
refused before any workflow carries it."""

import time
from collections.abc import Callable
from typing import Any

import httpx

from localization.config import ConfigError, Settings
from localization.providers.base import (
    LOCAL_CONNECT_TIMEOUT_SECONDS,
    OpenAICompatClient,
    local_server_http,
)
from localization.providers.errors import PermanentProviderError, TransientProviderError


class VoiceUnavailable(Exception):
    """The provider could not be asked, so the voice is neither confirmed nor refused."""


def parse_voice_ids(body: Any) -> set[str]:
    items = body.get("voices", body.get("data", [])) if isinstance(body, dict) else body
    if not isinstance(items, list):
        raise PermanentProviderError("voices: unexpected response shape")
    ids: set[str] = set()
    for item in items:
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict):
            value = item.get("id") or item.get("voice_id") or item.get("name")
            if isinstance(value, str):
                ids.add(value)
    return ids


class VoiceCatalog:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        timeout_seconds: float = 30.0,
        http: httpx.AsyncClient | None = None,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = OpenAICompatClient(
            "voices",
            base_url,
            api_key,
            timeout_seconds=timeout_seconds,
            http=http or local_server_http(),
            connect_timeout=LOCAL_CONNECT_TIMEOUT_SECONDS,
        )
        self._ttl = ttl_seconds
        self._clock = clock
        self._confirmed: dict[str, float] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        *,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> "VoiceCatalog":
        if not settings.voicestudio_url:
            raise ConfigError("VOICESTUDIO_URL must be set to validate VOICE_ID")
        key = settings.voicestudio_api_key
        return cls(
            settings.voicestudio_url,
            key.get_secret_value() if key else None,
            timeout_seconds=settings.provider_timeout_seconds,
            http=http,
            ttl_seconds=ttl_seconds,
            clock=clock,
        )

    async def voice_ids(self) -> set[str]:
        response = await self._client.get("/v1/audio/voices")
        return parse_voice_ids(self._client.json(response))

    async def ensure_exists(self, voice_id: str) -> None:
        """Returns when the provider lists `voice_id`. Raises `ConfigError` when it
        does not, and `VoiceUnavailable` when the provider could not answer. A
        confirmed id is remembered for the TTL, so dispatching does not call the
        provider once per video."""
        confirmed_at = self._confirmed.get(voice_id)
        if confirmed_at is not None and self._clock() - confirmed_at < self._ttl:
            return
        try:
            ids = await self.voice_ids()
        except (TransientProviderError, PermanentProviderError) as exc:
            raise VoiceUnavailable(str(exc)) from None
        if voice_id not in ids:
            raise ConfigError("VOICE_ID is not in the provider's voice list")
        self._confirmed[voice_id] = self._clock()
