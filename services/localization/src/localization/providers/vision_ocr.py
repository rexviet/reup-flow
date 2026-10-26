import base64
import json
import re

import httpx

from localization.config import BBoxFormat, Settings
from localization.contracts.geometry import BBox
from localization.providers.base import OpenAICompatClient
from localization.providers.bbox import normalize_bbox
from localization.providers.errors import PermanentProviderError

_PROMPT = (
    "Find the band of burned-in subtitle text in this video frame. Reply with JSON only: "
    '{"bbox": [a, b, c, d]} for the tightest box around the band, or {"bbox": null} when '
    "the frame has no burned-in subtitles."
)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _mime(image: bytes) -> str:
    return "image/jpeg" if image[:2] == b"\xff\xd8" else "image/png"


class VisionOcr:
    """Asks a vision model (chat completions) where the burned-in subtitle band is."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        bbox_format: BBoxFormat,
        *,
        timeout_seconds: float = 120.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._format = bbox_format
        self._client = OpenAICompatClient(
            "ocr", base_url, api_key, timeout_seconds=timeout_seconds, http=http
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, http: httpx.AsyncClient | None = None
    ) -> "VisionOcr":
        if not settings.ocr_model or not settings.ocr_bbox_format:
            raise PermanentProviderError("ocr: OCR_MODEL and OCR_BBOX_FORMAT must be set")
        key = settings.openrouter_api_key
        return cls(
            settings.openrouter_base_url,
            key.get_secret_value() if key else None,
            settings.ocr_model,
            settings.ocr_bbox_format,
            timeout_seconds=settings.provider_timeout_seconds,
            http=http,
        )

    async def detect_region(self, frame_png: bytes, width: int, height: int) -> BBox | None:
        image = base64.b64encode(frame_png).decode("ascii")
        response = await self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{_mime(frame_png)};base64,{image}"},
                            },
                        ],
                    }
                ],
            },
        )
        body = self._client.json(response)
        try:
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(_FENCE_RE.sub("", content.strip()))
            raw = parsed["bbox"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise PermanentProviderError("ocr: unexpected response shape") from None
        if raw is None:
            return None
        return normalize_bbox(raw, self._format, width, height)
