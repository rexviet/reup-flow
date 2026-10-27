"""The real workflow with room for a second rerun after a failed review, to see what
stops it when the limit does not."""

from temporalio import workflow

from localization.contracts import LocalizationInput, LocalizationResult
from localization.workflows.video_localization import VideoLocalizationWorkflow


@workflow.defn(name="VideoLocalizationWorkflow")
class ThreeReviewWorkflow(VideoLocalizationWorkflow):
    _review_rounds = 3

    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        return await super().run(spec)
