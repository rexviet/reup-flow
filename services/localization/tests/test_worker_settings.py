import pytest
from pydantic import ValidationError

from localization.config import ConfigError, Settings, voice_ref_from_settings


def test_defaults_match_the_environment_contract() -> None:
    s = Settings()
    assert (s.stt_concurrency, s.tts_concurrency, s.media_concurrency) == (1, 2, 2)
    assert (s.render_concurrency, s.llm_concurrency) == (1, 8)
    assert (s.max_open_workflows, s.outbox_max_attempts) == (10, 5)
    assert (s.target_lang, s.voice_provider, s.voice_settings_json) == ("vi", "voicestudio", "{}")


@pytest.mark.parametrize("value", ["0", "-1", "65", "x"])
def test_a_concurrency_limit_must_be_a_sane_positive_number(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TTS_CONCURRENCY", value)
    with pytest.raises(ValidationError):
        Settings()


def test_the_voice_is_any_id_with_the_providers_settings() -> None:
    ref = voice_ref_from_settings(
        Settings(voice_id="whatever-the-server-lists", voice_settings_json='{"speed": 1.2}')
    )
    assert ref.voice_id == "whatever-the-server-lists"
    assert ref.settings.speed == 1.2


def test_the_other_provider_gets_its_own_settings_class() -> None:
    ref = voice_ref_from_settings(Settings(voice_id="v", voice_provider="openai_compat"))
    assert ref.provider == "openai_compat"


@pytest.mark.parametrize("blob", ["not json", "[1]", '{"api_key": "sk-leak"}', '{"speed": 99}'])
def test_bad_settings_are_refused_without_echoing_them(blob: str) -> None:
    with pytest.raises(ConfigError) as caught:
        voice_ref_from_settings(Settings(voice_id="v", voice_settings_json=blob))
    assert "sk-leak" not in str(caught.value)
    assert blob not in str(caught.value)


def test_a_missing_voice_id_is_refused() -> None:
    with pytest.raises(ConfigError, match="VOICE_ID"):
        voice_ref_from_settings(Settings())
