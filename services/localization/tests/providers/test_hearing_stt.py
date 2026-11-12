import httpx
import pytest

from localization.config import Settings
from localization.providers.errors import PermanentProviderError
from localization.providers.openai_stt import OpenAICompatStt
from localization.providers.registry import build_providers

SEGMENTS = {"language": "vi", "segments": [{"start": 0, "end": 1, "text": " xin chào "}]}


def settings() -> Settings:
    return Settings(
        voice_provider="voicestudio",
        voicestudio_url="http://127.0.0.1:3900",
        voicestudio_allow_private_http_cidrs="127.0.0.0/8",
        stt_model="faster-whisper",
        tts_model="tts",
        openrouter_api_key="test-key",
        ocr_model="ocr",
        ocr_bbox_format="xyxy_px",
    )


async def test_hearing_sends_a_fixed_language_and_no_multilingual_flag():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=SEGMENTS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        hear = OpenAICompatStt("http://example.test", None, "faster-whisper", http=http)
        doc = await hear.transcribe(b"audio", language="vi")

    assert len(requests) == 1 and requests[0].url.path == "/v1/audio/transcriptions"
    body = requests[0].content
    assert b'name="language"\r\n\r\nvi' in body
    assert b'name="multilingual"' not in body
    assert doc.segments[0].text == "xin chào"


async def test_registry_builds_a_fixed_language_hearing_beside_the_multilingual_stt():
    providers = build_providers(settings())

    assert providers.stt is not None and providers.hear is not None
    assert providers.stt is not providers.hear
    assert providers.stt._multilingual is True
    assert providers.hear._multilingual is False
    with pytest.raises(PermanentProviderError, match="fixed language"):
        await providers.stt.transcribe(b"audio", language="vi")
