"""Unguarded edits of the Vbee dubbing workflow, to prove its replay can fail: one starts
the subtitle detection before the separation, the other no longer measures the rate on the
spoken clips."""

from temporalio import workflow

from localization.contracts import IngestResult, LocalizationInput, ScriptResult, SubtitleTrack
from localization.contracts.vbee_flow import SlotPlan, VbeeDubbingResult
from localization.workflows.vbee_dubbing import (
    DETECT_SUBTITLES,
    INGEST,
    WORKFLOW_NAME,
    VbeeDubbingWorkflow,
)


@workflow.defn(name=WORKFLOW_NAME)
class DetectFirstVbeeWorkflow(VbeeDubbingWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput):  # noqa: ANN201
        detecting = workflow.start_activity(
            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES
        )
        ingest = await workflow.execute_activity("ingest", spec, result_type=IngestResult, **INGEST)
        return ingest, await detecting


@workflow.defn(name=WORKFLOW_NAME)
class UncorrectedVbeeWorkflow(VbeeDubbingWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> VbeeDubbingResult:
        return await super().run(spec)

    async def _correct_rate(
        self,
        spec: LocalizationInput,
        plan: SlotPlan,
        heard: SlotPlan,
        subtitles: SubtitleTrack,
        script: ScriptResult,
    ) -> tuple[SlotPlan, ScriptResult]:
        return plan, script
