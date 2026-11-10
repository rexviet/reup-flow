from typing import Any

import httpx

from localization.config import Settings
from localization.contracts.models import SttSegment, SttWord, TranscriptDocument
from localization.observability.provider_spans import wav_seconds
from localization.providers.base import OpenAICompatClient
from localization.providers.errors import PermanentProviderError


def _ms(seconds: Any) -> int:
    return max(0, round(float(seconds) * 1000))


class OpenAICompatStt:
    """`POST /v1/audio/transcriptions` with `verbose_json`, word and segment
    granularity (VoiceStudio, or any server speaking the same dialect)."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        timeout_seconds: float = 120.0,
        http: httpx.AsyncClient | None = None,
        provider: str = "openai-compatible",
        multilingual: bool = False,
    ) -> None:
        self._model = model
        self._multilingual = multilingual
        self._client = OpenAICompatClient(
            "stt",
            base_url,
            api_key,
            timeout_seconds=timeout_seconds,
            http=http,
            provider=provider,
            model=model,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        *,
        multilingual: bool = False,
    ) -> "OpenAICompatStt":
        if not settings.voicestudio_url or not settings.stt_model:
            raise PermanentProviderError("stt: VOICESTUDIO_URL and STT_MODEL must be set")
        key = settings.voicestudio_api_key
        return cls(
            settings.voicestudio_url,
            key.get_secret_value() if key else None,
            settings.stt_model,
            timeout_seconds=settings.provider_timeout_seconds,
            http=http,
            provider=settings.voice_provider,
            multilingual=multilingual,
        )

    async def transcribe(
        self, audio: bytes, *, filename: str = "audio.wav", language: str | None = None
    ) -> TranscriptDocument:
        data: dict[str, str | list[str]] = {
            "model": self._model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
        }
        if self._multilingual:
            if language:
                raise PermanentProviderError("stt: multilingual cannot use a fixed language")
            await self._require_multilingual()
            data["multilingual"] = "true"
            data["temperature"] = "0"
        if language:
            data["language"] = language
        response = await self._client.post(
            "/v1/audio/transcriptions",
            units=wav_seconds(audio),
            data=data,
            files={"file": (filename, audio)},
        )
        return self._parse(self._client.json(response))

    async def _require_multilingual(self) -> None:
        # Recheck each request: a normal VoiceStudio restart can replace runtime patches.
        response = await self._client.get("/openapi.json")
        schema = self._client.json(response)
        try:
            body = schema["paths"]["/v1/audio/transcriptions"]["post"]["requestBody"]
            form = body["content"]["multipart/form-data"]["schema"]
            if "$ref" in form:
                form = schema["components"]["schemas"][form["$ref"].rsplit("/", 1)[-1]]
            supported = "multilingual" in form.get("properties", {})
        except (KeyError, TypeError, AttributeError):
            supported = False
        if not supported:
            raise PermanentProviderError(
                "stt: VoiceStudio API must expose multilingual; restore the patched runtime "
                "or update VoiceStudio before retrying"
            )

    @staticmethod
    def _parse(body: Any) -> TranscriptDocument:
        if not isinstance(body, dict) or not isinstance(body.get("segments"), list):
            raise PermanentProviderError("stt: unexpected response shape")
        try:
            raw_segments = body.get("segments") or []
            segments = tuple(
                SttSegment(
                    id=int(s.get("id", i)),
                    start_ms=_ms(s["start"]),
                    end_ms=_ms(s["end"]),
                    text=str(s.get("text", "")).strip(),
                )
                for i, s in enumerate(raw_segments)
            )
            raw_words = body.get("words")
            if raw_words is None:
                raw_words = [w for s in raw_segments for w in (s.get("words") or [])]
            words = tuple(
                SttWord(text=str(w["word"]).strip(), start_ms=_ms(w["start"]), end_ms=_ms(w["end"]))
                for w in raw_words
            )
            duration = body.get("duration")
            return TranscriptDocument(
                language=body.get("language"),
                duration_ms=_ms(duration) if duration is not None else None,
                segments=segments,
                words=words,
            )
        except (KeyError, TypeError, ValueError):
            raise PermanentProviderError("stt: unexpected response shape") from None
