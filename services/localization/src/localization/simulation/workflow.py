"""Synthetic orchestration with production activity names, for observing Workspace."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

NAMESPACE = "workspace-simulation-voicestudio"
QUEUE = "workspace-simulation"
STEPS = (
    ("check_voice", "voice_check"),
    ("ingest", "media"),
    ("voice_rate", "tts"),
    ("transcribe", "stt"),
    ("detect_subtitles", "io"),
    ("classify_video", "io"),
    ("agent__localizer_single__model_request", "llm"),
    ("assemble_script", "io"),
    ("voice_profile", "io"),
    ("plan_synthesis", "io"),
    ("synthesize_segments", "tts"),
    ("measure_fit", "io"),
    ("agent__dubber_code__model_request", "llm"),
    ("write_dubbed_script", "io"),
    ("plan_synthesis", "io"),
    ("synthesize_segments", "tts"),
    ("commit_dub", "io"),
    ("assemble_audio", "media"),
    ("layout_captions", "media"),
    # JEV is called inside this review activity, not a separate Temporal step.
    ("review_before_render", "io"),
    ("render", "render"),
    ("publish", "io"),
)


@workflow.defn
class WorkspaceSimulationWorkflow:
    @workflow.run
    async def run(self, scenario: str, seconds: float) -> None:
        # Keep completed/failed activities visible to the reader of open workflows.
        # Continue-as-new bounds history when a demo is left running overnight.
        for cycle in range(5):
            for index, (name, queue) in enumerate(STEPS):
                try:
                    await workflow.execute_activity(
                        name,
                        args=[scenario, seconds],
                        task_queue=queue,
                        activity_id=f"{cycle}-{index}",
                        start_to_close_timeout=timedelta(seconds=seconds + 15),
                        retry_policy=RetryPolicy(
                            initial_interval=timedelta(seconds=seconds),
                            maximum_interval=timedelta(seconds=seconds),
                            maximum_attempts=3,
                        ),
                    )
                except ActivityError:
                    # Intentional terminal activity failure; leave it visible for 30s.
                    await workflow.sleep(35)
                    break
            await workflow.sleep(10)
        workflow.continue_as_new(args=[scenario, seconds])
