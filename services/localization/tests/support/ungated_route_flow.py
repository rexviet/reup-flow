"""The real workflow asking whether a rerun could fix the review's problems with no
`patched()` marker: what an unguarded edit looks like to a run that was recorded before
the question existed."""

from temporalio import workflow

from localization.contracts import (
    DubbedAudio,
    LocalizationInput,
    LocalizationResult,
    LocalizedScriptRef,
    PrepResult,
    ReviewVerdict,
    SegmentRooms,
)
from localization.workflows.review_routing import NoRoute, Route, plan_rerun
from localization.workflows.video_localization import DUB_IO, VideoLocalizationWorkflow


@workflow.defn(name="VideoLocalizationWorkflow")
class UngatedRouteWorkflow(VideoLocalizationWorkflow):
    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        return await super().run(spec)

    async def _route(
        self, spec: LocalizationInput, prep: PrepResult, dub: DubbedAudio, verdict: ReviewVerdict
    ) -> Route | NoRoute:
        rooms = await workflow.execute_activity(
            "segment_room",
            args=[
                spec,
                LocalizedScriptRef(script_key=dub.script_key, version=dub.script_version),
                prep.ingest.duration_ms,
            ],
            result_type=SegmentRooms,
            **DUB_IO,
        )
        return plan_rerun(verdict.issues, rooms.items, frozenset())
