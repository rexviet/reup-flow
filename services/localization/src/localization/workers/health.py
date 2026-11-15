"""VoiceStudio liveness, checked every 30 seconds.

The result is only logged and counted, and the engine VoiceStudio reports as
active is kept for the TTS cache key. It gates nothing: a worker never refuses
to start, or to take a task, because VoiceStudio is down. An outage is a
transient activity failure, retried with backoff.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from opentelemetry import metrics

from localization.config import Settings

log = logging.getLogger(__name__)

WELL_KNOWN_PATH = "/.well-known/voicestudio-speech"
INTERVAL_SECONDS = 30.0

_checks = metrics.get_meter("localization.health").create_counter(
    "voicestudio.health.checks", description="VoiceStudio health checks by outcome"
)


@dataclass
class HealthState:
    ok: bool | None = None
    engine: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def active_engine(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for source in (body, body.get("tts"), body.get("speech")):
        if isinstance(source, dict):
            for name in ("active_engine", "engine"):
                value = source.get(name)
                if isinstance(value, str) and value:
                    return value
    return None


class VoiceStudioHealth:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}{WELL_KNOWN_PATH}"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = http or httpx.AsyncClient()
        self._timeout = timeout_seconds
        self.state = HealthState()

    @classmethod
    def from_settings(cls, settings: Settings) -> "VoiceStudioHealth | None":
        if not settings.voicestudio_url:
            return None
        key = settings.voicestudio_api_key
        return cls(settings.voicestudio_url, key.get_secret_value() if key else None)

    async def check_once(self) -> HealthState:
        try:
            response = await self._http.get(self._url, headers=self._headers, timeout=self._timeout)
            ok = response.status_code == 200
            engine = active_engine(response.json()) if ok else None
            outcome = "ok" if ok else f"http_{response.status_code}"
        except (httpx.HTTPError, ValueError):
            ok, engine, outcome = False, None, "unreachable"
        # The URL and headers never reach a log line: only the outcome does.
        if ok != self.state.ok:
            log.info("voicestudio health changed: %s (engine=%s)", outcome, engine)
        _checks.add(1, {"outcome": outcome})
        self.state = HealthState(ok=ok, engine=engine if ok else self.state.engine)
        return self.state

    async def run(self, interval_seconds: float = INTERVAL_SECONDS) -> None:
        while True:
            await self.check_once()
            await asyncio.sleep(interval_seconds)
