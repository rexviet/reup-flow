"""A one-activity workflow per provider kind, driving the REAL provider classes
(TTS, STT, OCR, object store), so failures travel the path they would in
production: provider error -> ApplicationError -> activity failure -> history."""

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from localization.config import Settings
    from localization.contracts import LocalizationInput
    from localization.providers.errors import ProviderError, to_application_error
    from localization.providers.openai_stt import OpenAICompatStt
    from localization.providers.openai_tts import OpenAICompatTts
    from localization.providers.vision_ocr import VisionOcr
    from localization.storage.object_store import ObjectStore

TASK_QUEUE = "canary"
KINDS = ("synthesize", "transcribe", "detect_region", "fetch_artifact")
ARTIFACT_KEY = "work/1/r0/artifact.bin"


def make_activities(settings: Settings, store: ObjectStore | None):
    @activity.defn(name="synthesize")
    async def synthesize(spec: LocalizationInput) -> int:
        try:
            return len(await OpenAICompatTts.from_settings(settings).synthesize("xin", spec.voice))
        except ProviderError as exc:
            raise to_application_error(exc) from None

    @activity.defn(name="transcribe")
    async def transcribe(spec: LocalizationInput) -> int:
        try:
            doc = await OpenAICompatStt.from_settings(settings).transcribe(b"RIFFdata")
        except ProviderError as exc:
            raise to_application_error(exc) from None
        return len(doc.segments)

    @activity.defn(name="detect_region")
    async def detect_region(spec: LocalizationInput) -> int:
        try:
            box = await VisionOcr.from_settings(settings).detect_region(b"\x89PNG", 1920, 1080)
        except ProviderError as exc:
            raise to_application_error(exc) from None
        return 0 if box is None else box.w

    @activity.defn(name="fetch_artifact")
    async def fetch_artifact(spec: LocalizationInput) -> int:
        assert store is not None
        return len(store.get_bytes(ARTIFACT_KEY, produced_by="transcribe"))

    return [synthesize, transcribe, detect_region, fetch_artifact]


@workflow.defn
class ProviderWorkflow:
    @workflow.run
    async def run(self, kind: str, spec: LocalizationInput) -> int:
        return await workflow.execute_activity(
            kind,
            spec,
            result_type=int,
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
