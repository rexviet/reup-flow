"""The real workflow checking the source for a voice, with no `patched()` marker: what an
unguarded edit looks like to a run recorded when every run began with the ingest. Only the
marker is missing; the check is the real workflow's."""

from temporalio import workflow

from localization.contracts import LocalizationInput, LocalizationResult
from localization.workflows.video_localization import VideoLocalizationWorkflow


@workflow.defn(name="VideoLocalizationWorkflow")
class UngatedVoiceCheckWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        return await super().run(spec)

    def _checks_for_voice(self) -> bool:
        return True
