from typing import Any

import httpx
from pydantic import ValidationError

from localization.config import Settings
from localization.contracts.models import SttSegment, SttWord, TranscriptDocument
from localization.observability.provider_spans import wav_seconds
from localization.providers.base import (
    LOCAL_CONNECT_TIMEOUT_SECONDS,
    OpenAICompatClient,
    local_server_http,
)
from localization.providers.errors import PermanentProviderError, TransientProviderError


def _shape(body: Any) -> str:
    """What the answer was, never what it said: its type and, for an object, its key names. A
    body can echo what was sent, and the reason must be enough to tell a wrong answer from a
    cut one when the same call is seen again."""
    if isinstance(body, dict):
        keys = ",".join(sorted(str(k) for k in body)[:10]) or "none"
        return f"object with keys: {keys}"
    return type(body).__name__


def _ms(seconds: Any) -> int:
    return max(0, round(float(seconds) * 1000))


class _Unreadable(Exception):
    """An item of the answer that cannot be read; the message says which one and what is
    wrong with it, never what it said."""


def _number(value: Any) -> str:
    """A time as the server sent it, when it is a number; its type otherwise."""
    return repr(value) if isinstance(value, int | float) else type(value).__name__


def _seconds(item: dict, key: str, where: str) -> int:
    if key not in item:
        raise _Unreadable(f"{where} has no '{key}'")
    try:
        return _ms(item[key])
    except (TypeError, ValueError, OverflowError):
        raise _Unreadable(f"{where}.{key} is not a usable number ({_number(item[key])})") from None


def _word_text(word: dict, where: str) -> str:
    if "word" not in word:
        raise _Unreadable(f"{where} has no 'word'")
    return str(word["word"]).strip()


def _read_item(kind: str, index: int, item: Any, build: Any) -> Any:
    """`build(item, where)` for one entry of `segments` or `words`, or `_Unreadable` naming the
    entry (`segments[3]`), the field and what is wrong with it. A time that ends before it
    starts also gives both times: numbers from the server, not its text."""
    where = f"{kind}[{index}]"
    if not isinstance(item, dict):
        raise _Unreadable(f"{where} is a {type(item).__name__}, not an object")
    try:
        return build(item, where)
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "item"
        detail = f"{where}.{field} fails its check ({first['type']})"
        if "start" in item and "end" in item:
            detail += f"; start {_number(item['start'])}, end {_number(item['end'])}"
        raise _Unreadable(detail) from None
    except (KeyError, TypeError, ValueError) as error:
        raise _Unreadable(f"{where} could not be read ({type(error).__name__})") from None


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
        temperature: float | None = None,
    ) -> None:
        self._model = model
        self._multilingual = multilingual
        self._temperature = temperature
        self._client = OpenAICompatClient(
            "stt",
            base_url,
            api_key,
            timeout_seconds=timeout_seconds,
            http=http or local_server_http(),
            connect_timeout=LOCAL_CONNECT_TIMEOUT_SECONDS,
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
        temperature: float | None = None,
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
            temperature=temperature,
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
        elif self._temperature is not None:
            data["temperature"] = str(self._temperature)
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
        # A 200 whose body cannot be read says nothing about the next request: a server that is
        # loading a model or overloaded answers like this, and the same chunk read fine on the
        # next call. Retrying is cheap; calling it permanent ended a 14-minute video's run.
        if not isinstance(body, dict) or not isinstance(body.get("segments"), list):
            raise TransientProviderError(f"stt: unexpected response shape ({_shape(body)})")
        try:
            raw_segments = body["segments"]
            segments = tuple(
                _read_item(
                    "segments",
                    i,
                    s,
                    lambda s, where, i=i: SttSegment(
                        id=int(s.get("id", i)),
                        start_ms=_seconds(s, "start", where),
                        end_ms=_seconds(s, "end", where),
                        text=str(s.get("text", "")).strip(),
                    ),
                )
                for i, s in enumerate(raw_segments)
            )
            raw_words = body.get("words")
            if raw_words is None:
                raw_words = [w for s in raw_segments for w in (s.get("words") or [])]
            words = tuple(
                _read_item(
                    "words",
                    i,
                    w,
                    lambda w, where: SttWord(
                        text=_word_text(w, where),
                        start_ms=_seconds(w, "start", where),
                        end_ms=_seconds(w, "end", where),
                    ),
                )
                for i, w in enumerate(raw_words)
            )
            duration = body.get("duration")
            return TranscriptDocument(
                language=body.get("language"),
                duration_ms=_ms(duration) if duration is not None else None,
                segments=segments,
                words=words,
            )
        except _Unreadable as error:
            raise TransientProviderError(
                f"stt: unexpected response shape ({_shape(body)}; {error})"
            ) from None
        except (KeyError, TypeError, ValueError):
            raise TransientProviderError(
                f"stt: unexpected response shape ({_shape(body)}, unreadable segments or words)"
            ) from None
