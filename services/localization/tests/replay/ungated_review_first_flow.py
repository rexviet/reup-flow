"""The real workflow reviewing each round before the render, and rendering once after the
last, with no `patched()` marker: what an unguarded edit looks like to a run recorded when
every round was rendered and the render reviewed. Only the marker is missing; the rounds
are the real workflow's."""

from temporalio import workflow

from localization.contracts import LocalizationInput, LocalizationResult
from localization.workflows.video_localization import VideoLocalizationWorkflow


@workflow.defn(name="VideoLocalizationWorkflow")
class UngatedReviewFirstWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        return await super().run(spec)

    def _review_first(self) -> bool:
        return True
