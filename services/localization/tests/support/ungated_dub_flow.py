"""The real workflow with the dubbing stage added and no `patched()` marker: what an
unguarded edit looks like to a run that was recorded before the stage existed."""

from temporalio import workflow

from localization.contracts import LocalizationInput, LocalizationResult
from localization.workflows.video_localization import VideoLocalizationWorkflow


@workflow.defn(name="VideoLocalizationWorkflow")
class UngatedDubWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        prep = await self._prepare(spec)
        if not workflow.patched("localize-script"):
            return LocalizationResult(prep=prep, script=None)
        prep, script = await self._localize(spec, prep)
        dub = await self._dub_with_regeneration(spec, prep, script)
        return LocalizationResult(prep=prep, script=script, dub=dub)
