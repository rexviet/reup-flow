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
    stt: SttProvider
    tts: TtsProvider
    ocr: OcrProvider
    demucs: StemSeparator


def build_providers(settings: Settings, http: httpx.AsyncClient | None = None) -> Providers:
    """Builds every provider from configuration. A missing setting fails here,
    at startup, rather than on the first video."""
    return Providers(
        stt=OpenAICompatStt.from_settings(settings, http),
        tts=OpenAICompatTts.from_settings(settings, http),
        ocr=VisionOcr.from_settings(settings, http),
        demucs=DemucsSeparator(settings.demucs_bin),
    )
