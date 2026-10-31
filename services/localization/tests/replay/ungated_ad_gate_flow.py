"""The real workflow asking whether the video is an advertisement, with no `patched()`
marker: what an unguarded edit looks like to a run recorded when a prepared video went
straight to the Localizer. Only the marker is missing; the check is the real workflow's."""

from temporalio import workflow

from localization.contracts import LocalizationInput, LocalizationResult
from localization.workflows.video_localization import VideoLocalizationWorkflow


@workflow.defn(name="VideoLocalizationWorkflow")
class UngatedAdGateWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        return await super().run(spec)

    def _asks_about_ads(self) -> bool:
        return True
