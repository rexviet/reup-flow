"""A one-activity workflow that drives the REAL TTS provider class, so failures
travel the same path they would in production: provider error -> ApplicationError
-> activity failure -> workflow history."""

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from localization.config import Settings
    from localization.contracts import LocalizationInput
    from localization.providers.errors import ProviderError, to_application_error
    from localization.providers.openai_tts import OpenAICompatTts

TASK_QUEUE = "canary"


def make_synthesize_activity(settings: Settings):
    @activity.defn(name="synthesize")
    async def synthesize(spec: LocalizationInput) -> int:
        tts = OpenAICompatTts.from_settings(settings)
        try:
            audio = await tts.synthesize("xin chao", spec.voice)
        except ProviderError as exc:
            raise to_application_error(exc) from None
        return len(audio)

    return synthesize


@workflow.defn
class SynthesizeWorkflow:
    @workflow.run
    async def run(self, spec: LocalizationInput) -> int:
        return await workflow.execute_activity(
            "synthesize",
            spec,
            result_type=int,
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
