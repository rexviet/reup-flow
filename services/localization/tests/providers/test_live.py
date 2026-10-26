"""Calls the real VoiceStudio and OpenRouter. Run by hand with the env set:

uv run pytest tests/providers -m live
"""

import pytest

from localization.config import Settings, voice_ref_from_settings
from localization.providers.llm import model_for
from localization.providers.openai_stt import OpenAICompatStt
from localization.providers.openai_tts import OpenAICompatTts

pytestmark = pytest.mark.live


async def test_voicestudio_speaks_and_transcribes_back() -> None:
    settings = Settings()
    if not (settings.voicestudio_url and settings.tts_model and settings.stt_model):
        pytest.skip("VOICESTUDIO_URL, TTS_MODEL and STT_MODEL are not set")
    audio = await OpenAICompatTts.from_settings(settings).synthesize(
        "Xin chào các bạn", voice_ref_from_settings(settings)
    )
    assert audio[:4] == b"RIFF"
    doc = await OpenAICompatStt.from_settings(settings).transcribe(audio, language="vi")
    assert doc.segments


async def test_openrouter_model_answers() -> None:
    settings = Settings()
    if not (settings.openrouter_api_key and settings.localizer_model):
        pytest.skip("OPENROUTER_API_KEY and LOCALIZER_MODEL are not set")
    from pydantic_ai import Agent

    result = await Agent(model_for("localizer", settings)).run("Reply with the single word: ok")
    assert result.output.strip()
