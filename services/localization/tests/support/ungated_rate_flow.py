"""The real workflow measuring the voice's rate beside the ingest with no `patched()`
marker: what an unguarded edit looks like to a run that was recorded before the rate was
measured there. Replay compares commands as the workflow issues them, so the first
command that differs from the recording is all it takes; what follows does not matter."""

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from localization.contracts import LocalizationInput, LocalizationResult, PrepResult
from localization.workflows.video_localization import VideoLocalizationWorkflow


@workflow.defn(name="VideoLocalizationWorkflow")
class UngatedRateWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        return await super().run(spec)

    async def _prepare(self, spec: LocalizationInput) -> PrepResult:
        await self._ingest_beside_rate(spec)
        raise ApplicationError("not reached on replay", non_retryable=True)
