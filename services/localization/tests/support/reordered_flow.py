"""The preparation workflow with 2a and 2b started in the opposite order and no
`patched()` marker: what an unguarded edit of the real workflow looks like."""

from temporalio import workflow
from temporalio.workflow import ActivityHandle

from localization.contracts import (
    IngestResult,
    LocalizationInput,
    PrepResult,
    SubtitleTrack,
    Transcript,
)
from localization.workflows.video_localization import (
    DETECT_SUBTITLES,
    INGEST,
    TRANSCRIBE,
)


@workflow.defn(name="VideoLocalizationWorkflow")
class ReorderedWorkflow:
    @workflow.run
    async def run(self, spec: LocalizationInput) -> PrepResult:
        ingest = await workflow.execute_activity("ingest", spec, result_type=IngestResult, **INGEST)
        detecting: ActivityHandle = workflow.start_activity(
            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES
        )
        transcribing: ActivityHandle = workflow.start_activity(
            "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE
        )
        return PrepResult(ingest=ingest, transcript=await transcribing, subtitles=await detecting)
