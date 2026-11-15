"""The real workflow asking the Localizer's context with no `patched()` marker: what an
unguarded edit looks like to a run that was recorded before the one-request mode existed."""

from temporalio import workflow

from localization.agents.localizer_single import LocalizerContext
from localization.agents.temporal import CONTEXT_ACTIVITY, LocalizerDeps
from localization.contracts import LocalizationInput, LocalizationResult, PrepResult, ScriptResult
from localization.workflows.video_localization import VideoLocalizationWorkflow


@workflow.defn(name="VideoLocalizationWorkflow")
class UngatedSingleWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        return await super().run(spec)

    async def _localize_once(self, spec: LocalizationInput, prep: PrepResult) -> ScriptResult:
        await workflow.execute_activity(
            "localizer_context",
            LocalizerDeps(
                aweme_id=spec.aweme_id,
                workflow_id=workflow.info().workflow_id,
                channel_id=spec.channel_id,
                transcript_key=prep.transcript.transcript_key,
                subtitle_key=prep.subtitles.subtitle_key,
                syllables_per_sec=4.0,
                segment_count=prep.transcript.segment_count,
            ),
            result_type=LocalizerContext | None,
            **CONTEXT_ACTIVITY,
        )
        return await super()._localize_once(spec, prep)
