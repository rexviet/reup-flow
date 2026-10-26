from dataclasses import dataclass

import httpx

from localization.config import Settings
from localization.providers.base import OcrProvider, StemSeparator, SttProvider, TtsProvider
from localization.providers.demucs import DemucsSeparator
from localization.providers.openai_stt import OpenAICompatStt
from localization.providers.openai_tts import OpenAICompatTts
from localization.providers.vision_ocr import VisionOcr


@dataclass(frozen=True)
class Providers:
    """What a worker role needs. A role builds only the providers it uses: the
    media worker has Demucs and nothing else, so it needs no provider settings."""

    stt: SttProvider | None = None
    tts: TtsProvider | None = None
    ocr: OcrProvider | None = None
    demucs: StemSeparator | None = None


def build_providers(settings: Settings, http: httpx.AsyncClient | None = None) -> Providers:
    """Builds every provider from configuration. A missing setting fails here,
    at startup, rather than on the first video."""
    return Providers(
        stt=OpenAICompatStt.from_settings(settings, http),
        tts=OpenAICompatTts.from_settings(settings, http),
        ocr=VisionOcr.from_settings(settings, http),
        demucs=DemucsSeparator(settings.demucs_bin),
    )


def build_media_providers(settings: Settings) -> Providers:
    return Providers(demucs=DemucsSeparator(settings.demucs_bin))
