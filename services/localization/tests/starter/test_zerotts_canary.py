"""A review canary can be spoken with a ZeroTTS voice for one run, without changing the
configured voice or any other run."""

import pytest

from localization.config import ConfigError, Settings
from localization.contracts.voice import VoiceRef, ZeroTtsSettings
from localization.starter.outbox_starter import OutboxStarter, zerotts_voice


def test_the_voice_option_names_a_zerotts_voice_by_its_id() -> None:
    voice = zerotts_voice("zerotts:3k7WSOHjfuY9euT7VKCA")
    assert voice == VoiceRef(
        provider="zerotts", voice_id="3k7WSOHjfuY9euT7VKCA", settings=ZeroTtsSettings()
    )


@pytest.mark.parametrize("given", ["3k7WSOHjfuY9euT7VKCA", "zerotts:", "zerotts:  ", "vbee:x"])
def test_anything_but_zerotts_and_an_id_is_refused(given: str) -> None:
    with pytest.raises(ConfigError, match="--voice"):
        zerotts_voice(given)


async def test_a_canary_with_a_voice_that_has_no_review_pipeline_is_refused_before_the_database():
    settings = Settings(localization_pipeline="voicestudio")
    starter = OutboxStarter("unused", object(), load=lambda: settings)
    openai = VoiceRef.model_validate(dict(provider="openai_compat", voice_id="alloy", settings={}))
    with pytest.raises(ConfigError, match="VoiceStudio or ZeroTTS"):
        await starter.start_canary("7001", 2, voice=openai)


def test_new_workflows_speak_with_the_zerotts_voice_when_one_is_set() -> None:
    from localization.starter.outbox_starter import run_voice

    settings = Settings(zerotts_voice_id="3k7WSOHjfuY9euT7VKCA", voice_id="alloy")
    assert run_voice(settings) == VoiceRef(
        provider="zerotts", voice_id="3k7WSOHjfuY9euT7VKCA", settings=ZeroTtsSettings()
    )


@pytest.mark.parametrize("empty", [None, "", "  "])
def test_without_a_zerotts_voice_the_configured_voicestudio_voice_is_used(empty) -> None:
    from localization.starter.outbox_starter import run_voice

    voice = run_voice(Settings(zerotts_voice_id=empty, voice_id="alloy"))
    assert (voice.provider, voice.voice_id) == ("voicestudio", "alloy")


def test_the_vbee_pipeline_ignores_the_zerotts_voice() -> None:
    from localization.starter.outbox_starter import run_voice

    settings = Settings(localization_pipeline="vbee", zerotts_voice_id="3k7WSOHjfuY9euT7VKCA")
    with pytest.raises(ConfigError, match="VBEE"):
        run_voice(settings)
