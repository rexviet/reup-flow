import pytest
from pydantic import ValidationError

from localization.config import ConfigError, Settings, voice_ref_from_settings


def test_defaults_match_the_environment_contract() -> None:
    s = Settings()
    assert (s.stt_concurrency, s.tts_concurrency, s.media_concurrency) == (1, 2, 2)
    assert (s.render_concurrency, s.llm_concurrency) == (1, 8)
    assert (s.max_open_workflows, s.outbox_max_attempts) == (10, 5)
    assert (s.target_lang, s.voice_provider, s.voice_settings_json) == ("vi", "voicestudio", "{}")


def test_the_loudness_defaults_match_the_environment_contract() -> None:
    s = Settings()
    assert (s.loudness_lufs, s.true_peak_dbtp) == (-14.0, -1.5)


def test_the_loudness_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOUDNESS_LUFS", "-16")
    monkeypatch.setenv("TRUE_PEAK_DBTP", "-2")
    s = Settings()
    assert (s.loudness_lufs, s.true_peak_dbtp) == (-16.0, -2.0)


@pytest.mark.parametrize(("name", "value"), [("LOUDNESS_LUFS", "5"), ("TRUE_PEAK_DBTP", "3")])
def test_a_loudness_above_full_scale_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


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
