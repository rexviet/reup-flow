"""Records contextual histories before durable review delivery existed."""

from temporalio import workflow

from localization.contracts import LocalizationInput, LocalizationResult
from localization.workflows.video_localization import VideoLocalizationWorkflow


@workflow.defn(name="VideoLocalizationWorkflow")
class BeforeReviewProjectsWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        return await super().run(spec)

    def _delivers_project(self, spec):
        return False
