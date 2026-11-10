import httpx
import pytest

from localization.providers.errors import PermanentProviderError
from localization.providers.openai_stt import OpenAICompatStt


@pytest.mark.parametrize("supported", [True, False])
async def test_multilingual_requires_advertised_flag_and_sends_auto_language(supported):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "paths": {
                        "/v1/audio/transcriptions": {
                            "post": {
                                "requestBody": {
                                    "content": {
                                        "multipart/form-data": {
                                            "schema": {"$ref": "#/components/schemas/Form"}
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "components": {
                        "schemas": {
                            "Form": {
                                "properties": {"multilingual": {"type": "boolean"}}
                                if supported
                                else {}
                            }
                        }
                    },
                },
            )
        return httpx.Response(200, json={"language": "zh", "segments": [], "words": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        stt = OpenAICompatStt(
            "http://example.test", None, "faster-whisper", http=http, multilingual=True
        )
        if supported:
            await stt.transcribe(b"audio")
            body = requests[-1].content
            assert b'name="multilingual"\r\n\r\ntrue' in body
            assert b'name="temperature"\r\n\r\n0' in body
            assert b'name="language"' not in body
        else:
            with pytest.raises(PermanentProviderError, match="must expose multilingual"):
                await stt.transcribe(b"audio")
            assert len(requests) == 1


async def test_fixed_language_conflicts_before_sending_any_request():
    stt = OpenAICompatStt("http://example.test", None, "model", multilingual=True)
    with pytest.raises(PermanentProviderError, match="fixed language"):
        await stt.transcribe(b"audio", language="zh")


@pytest.mark.parametrize("provider,enabled", [("voicestudio", True), ("openai_compat", False)])
def test_production_provider_enables_multilingual_only_for_voice_studio(provider, enabled):
    from localization.config import Settings
    from localization.providers.registry import build_providers

    settings = Settings(
        voice_provider=provider,
        voicestudio_url="http://127.0.0.1:3900",
        voicestudio_allow_private_http_cidrs="127.0.0.0/8",
        stt_model="faster-whisper",
        tts_model="tts",
        openrouter_api_key="test-key",
        ocr_model="ocr",
        ocr_bbox_format="xyxy_px",
    )
    assert build_providers(settings).stt._multilingual is enabled
