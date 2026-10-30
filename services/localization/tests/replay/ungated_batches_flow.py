"""The real workflow splitting the texts to synthesize into even batches with no
`patched()` marker: what an unguarded edit looks like to a run recorded before the split.
It finishes the synthesis like the real workflow, so the split is the only thing that can
differ from the recording."""

from temporalio import workflow

from localization.contracts import LocalizationInput, LocalizationResult, SynthesisPlan
from localization.workflows.video_localization import (
    DUB_IO,
    SYNTHESIZE,
    VideoLocalizationWorkflow,
    tts_batches,
)


@workflow.defn(name="VideoLocalizationWorkflow")
class UngatedBatchesWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        return await super().run(spec)

    async def _synthesize(
        self, spec: LocalizationInput, engine: str | None, script_key: str
    ) -> None:
        plan = await workflow.execute_activity(
            "plan_synthesis",
            args=[spec, script_key, engine],
            result_type=SynthesisPlan,
            **DUB_IO,
        )
        handles = [
            workflow.start_activity(
                "synthesize_segments", args=[spec, engine, script_key, batch], **SYNTHESIZE
            )
            for batch in tts_batches(list(plan.hashes))
        ]
        for handle in handles:
            await handle
