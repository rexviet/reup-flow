from dataclasses import dataclass

import httpx

from localization.config import ConfigError, Settings
from localization.providers.base import OcrProvider, StemSeparator, SttProvider, TtsProvider
from localization.providers.demucs import DemucsSeparator
from localization.providers.openai_stt import OpenAICompatStt
from localization.providers.openai_tts import OpenAICompatTts
from localization.providers.vbee import VbeeStt, VbeeTts
from localization.providers.vision_ocr import VisionOcr


@dataclass(frozen=True)
class Providers:
    """What a worker role needs. A role builds only the providers it uses: the
    media worker has Demucs and nothing else, so it needs no provider settings."""

    stt: SttProvider | None = None
    tts: TtsProvider | None = None
    ocr: OcrProvider | None = None
    demucs: StemSeparator | None = None
    # Vbee's speech, for the Vbee dubbing workflow; None on a worker without its credentials.
    vbee_stt: VbeeStt | None = None
    vbee_tts: VbeeTts | None = None


def vbee_configured(settings: Settings) -> bool:
    return all(
        s is not None and s.get_secret_value()
        for s in (settings.vbee_app_id, settings.vbee_api_token, settings.vbee_voice_id)
    )


def build_providers(
    settings: Settings,
    http: httpx.AsyncClient | None = None,
    llm_http: httpx.AsyncClient | None = None,
) -> Providers:
    """Builds every provider from configuration. A missing setting fails here,
    at startup, rather than on the first video: VoiceStudio's for the VoiceStudio
    pipeline, Vbee's for the Vbee one. The other pipeline's providers are built when
    they are configured (runs of it may still be in flight), and are None otherwise."""
    vbee = settings.localization_pipeline == "vbee"
    if vbee and not vbee_configured(settings):
        raise ConfigError(
            "LOCALIZATION_PIPELINE=vbee needs VBEE_APP_ID, VBEE_API_TOKEN and VBEE_VOICE_ID"
        )
    voicestudio = not vbee or bool(settings.voicestudio_url)
    with_vbee = vbee_configured(settings)
    return Providers(
        stt=OpenAICompatStt.from_settings(
            settings, http, multilingual=settings.voice_provider == "voicestudio"
        )
        if voicestudio
        else None,
        tts=OpenAICompatTts.from_settings(settings, http) if voicestudio else None,
        ocr=VisionOcr.from_settings(settings, llm_http or http),
        demucs=DemucsSeparator(settings.demucs_bin),
        vbee_stt=VbeeStt.from_settings(settings, http) if with_vbee else None,
        vbee_tts=VbeeTts.from_settings(settings, http) if with_vbee else None,
    )


def build_media_providers(settings: Settings) -> Providers:
    return Providers(demucs=DemucsSeparator(settings.demucs_bin))
