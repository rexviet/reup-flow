import httpx

from localization.config import Settings
from localization.contracts.voice import VoiceRef
from localization.providers.base import OpenAICompatClient
from localization.providers.errors import PermanentProviderError


class OpenAICompatTts:
    """`POST /v1/audio/speech` with `model=TTS_MODEL` and `voice=voice_id`."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        timeout_seconds: float = 120.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = OpenAICompatClient(
            "tts", base_url, api_key, timeout_seconds=timeout_seconds, http=http
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, http: httpx.AsyncClient | None = None
    ) -> "OpenAICompatTts":
        if not settings.voicestudio_url or not settings.tts_model:
            raise PermanentProviderError("tts: VOICESTUDIO_URL and TTS_MODEL must be set")
        key = settings.voicestudio_api_key
        return cls(
            settings.voicestudio_url,
            key.get_secret_value() if key else None,
            settings.tts_model,
            timeout_seconds=settings.provider_timeout_seconds,
            http=http,
        )

    async def synthesize(self, text: str, voice: VoiceRef) -> bytes:
        payload: dict[str, object] = {
            "model": self._model,
            "input": text,
            "voice": voice.voice_id,
            "response_format": "wav",
            **voice.settings.model_dump(exclude_none=True),
        }
        response = await self._client.post("/v1/audio/speech", json=payload)
        audio = response.content
        # The result is cached forever under a hash of its inputs, so anything that
        # is not a WAV (a 200 carrying a JSON error, an empty body) must never be
        # stored as if it were audio.
        if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise PermanentProviderError("tts: response is not a WAV file")
        return audio
