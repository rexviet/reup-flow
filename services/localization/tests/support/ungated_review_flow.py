"""The real workflow with the review and publish steps added and no `patched()` marker:
what an unguarded edit looks like to a run that was recorded before they existed."""

from temporalio import workflow

from localization.contracts import LocalizationInput, LocalizationResult
from localization.workflows.video_localization import (
    DUB_PATCH,
    LOCALIZE_PATCH,
    VideoLocalizationWorkflow,
)


@workflow.defn(name="VideoLocalizationWorkflow")
class UngatedReviewWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        prep = await self._prepare(spec)
        if not workflow.patched(LOCALIZE_PATCH):
            return LocalizationResult(prep=prep, script=None)
        prep, script = await self._localize(spec, prep)
        if not workflow.patched(DUB_PATCH):
            return LocalizationResult(prep=prep, script=script)
        prep, script, dub = await self._dub_with_regeneration(spec, prep, script)
        script, dub, published = await self._review_and_publish(spec, prep, script, dub)
        return LocalizationResult(prep=prep, script=script, dub=dub, publish=published)
