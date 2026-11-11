import asyncio
import base64
import json
import re

import httpx

from localization.config import Settings
from localization.providers.base import OpenAICompatClient
from localization.providers.errors import TransientProviderError

# Two different questions about the same clip; a clip is sung only when both say so. Measured on
# one video (27 clips): either question alone on the original audio had no mistakes, on the
# separated vocals each read one spoken line as sung.
LABEL_PROMPT = (
    "Listen to this audio clip from a video. Decide whether the human voice in it is SINGING "
    "(sung lyrics over a melody) or SPEECH (spoken dialogue, shouting, crying, talking). "
    'Reply with JSON only: {"label": "singing"} or {"label": "speech"}.'
)
MELODY_PROMPT = (
    "Listen to this clip. Does the voice hold sustained pitched notes that follow a musical "
    "melody (as in a song), rather than the rising and falling intonation of ordinary talking, "
    "shouting or acting? "
    'Reply with JSON only: {"melodic": true} or {"melodic": false}.'
)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


class SingingDetector:
    """Asks an audio-capable model (chat completions, `input_audio`) whether a vocals
    clip is sung."""

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
            "singing",
            base_url,
            api_key,
            timeout_seconds=timeout_seconds,
            http=http,
            provider="openrouter",
            model=model,
        )

    @property
    def model_id(self) -> str:
        return self._model

    async def is_singing(self, wav: bytes) -> bool:
        """True only when both questions say the clip is sung."""
        label, melodic = await asyncio.gather(
            self._ask(wav, LABEL_PROMPT, "label"), self._ask(wav, MELODY_PROMPT, "melodic")
        )
        if label not in ("singing", "speech") or not isinstance(melodic, bool):
            raise TransientProviderError("singing: the reply is not the requested JSON")
        return label == "singing" and melodic

    async def _ask(self, wav: bytes, prompt: str, field: str) -> object:
        response = await self._client.post(
            "/chat/completions",
            units=1,
            json={
                "model": self._model,
                # The answer is a few tokens of JSON; reasoning would only cost.
                "reasoning": {"enabled": False},
                "max_tokens": 50,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": base64.b64encode(wav).decode("ascii"),
                                    "format": "wav",
                                },
                            },
                        ],
                    }
                ],
            },
        )
        try:
            raw = self._client.json(response)["choices"][0]["message"]["content"]
            return json.loads(_FENCE_RE.sub("", raw.strip()))[field]
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            # A fresh call with the same clip can repair a malformed reply; answers already
            # given are cached by the caller.
            raise TransientProviderError("singing: the reply is not the requested JSON") from None

    @classmethod
    def from_settings(
        cls, settings: Settings, http: httpx.AsyncClient | None = None
    ) -> "SingingDetector | None":
        """None when no `SINGING_MODEL` is set: nothing is asked, every segment is speech."""
        if not settings.singing_model:
            return None
        key = settings.openrouter_api_key
        return cls(
            settings.openrouter_base_url,
            key.get_secret_value() if key else None,
            settings.singing_model,
            timeout_seconds=settings.provider_timeout_seconds,
            http=http,
        )
