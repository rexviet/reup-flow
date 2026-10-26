"""The video localization workflow. Deterministic: it decides the order of
activities and nothing else. Every read and write of data happens inside an
activity, and activities are named by string so this module imports no I/O code.

Any change to the ORDER of the commands this workflow issues must be guarded with
`workflow.patched("<name>")`, or a run that is already in flight breaks on replay.
"""

from collections.abc import Callable
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from localization.contracts import (
    IngestResult,
    LocalizationInput,
    PrepResult,
    SubtitleTrack,
    Transcript,
)

# Task queues. `workflows` carries only this workflow; every activity runs elsewhere.
MEDIA_QUEUE = "media"
STT_QUEUE = "stt"
IO_QUEUE = "io"

ARTIFACT_MISSING = "ArtifactMissingError"
# The stages whose artifacts the workflow can regenerate (once) when one is lost.
REGENERABLE_STAGES = frozenset({"ingest"})

# Not worth retrying: the same input fails the same way.
_PERMANENT = ["PermanentProviderError", "PayloadTooLarge", ARTIFACT_MISSING]


def _retry(attempts: int, first: float, cap: float) -> RetryPolicy:
    return RetryPolicy(
        maximum_attempts=attempts,
        initial_interval=timedelta(seconds=first),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=cap),
        non_retryable_error_types=_PERMANENT,
    )


INGEST = dict(
    task_queue=MEDIA_QUEUE,
    start_to_close_timeout=timedelta(minutes=45),
    heartbeat_timeout=timedelta(minutes=2),
    retry_policy=_retry(3, 10, 120),
)
TRANSCRIBE = dict(
    task_queue=STT_QUEUE,
    start_to_close_timeout=timedelta(minutes=30),
    # Provider outages (429, 5xx, a busy GPU) are transient: retry with backoff
    # that tops out at five minutes.
    retry_policy=_retry(8, 5, 300),
)
DETECT_SUBTITLES = dict(
    task_queue=IO_QUEUE,
    start_to_close_timeout=timedelta(minutes=45),
    heartbeat_timeout=timedelta(minutes=2),
    retry_policy=_retry(5, 5, 300),
)


# `asyncio.FIRST_EXCEPTION`, spelled out because workflow code does not import asyncio.
_FIRST_EXCEPTION = "FIRST_EXCEPTION"


def _failed(handle: workflow.ActivityHandle) -> bool:
    return handle.done() and not handle.cancelled() and handle.exception() is not None


def _missing_stage(error: ActivityError) -> str | None:
    cause = error.cause
    if isinstance(cause, ApplicationError) and cause.type == ARTIFACT_MISSING and cause.details:
        stage = cause.details[0]
        return stage if isinstance(stage, str) else None
    return None


@workflow.defn(name="VideoLocalizationWorkflow")
class VideoLocalizationWorkflow:
    def __init__(self) -> None:
        self._regenerated: set[str] = set()

    @workflow.run
    async def run(self, spec: LocalizationInput) -> PrepResult:
        ingest = await self._ingest(spec)
        # 2a and 2b are independent; both are started before either is awaited.
        transcribing = workflow.start_activity(
            "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE
        )
        detecting = workflow.start_activity(
            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES
        )
        await workflow.wait([transcribing, detecting], return_when=_FIRST_EXCEPTION)
        stages = {
            "transcribe": (
                transcribing,
                lambda: workflow.start_activity(
                    "transcribe",
                    args=[spec, ingest.vocals_key],
                    result_type=Transcript,
                    **TRANSCRIBE,
                ),
            ),
            "detect": (
                detecting,
                lambda: workflow.start_activity(
                    "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES
                ),
            ),
        }
        # A stage that already failed is settled first, so a failure is acted on as
        # soon as it happens instead of after the slower sibling finishes.
        order = sorted(stages, key=lambda n: not _failed(stages[n][0]))
        results = {}
        try:
            for name in order:
                handle, restart = stages[name]
                results[name] = await self._settle(handle, restart, spec)
        except BaseException:
            # The sibling's result can no longer be used: stop paying for it.
            for handle, _ in stages.values():
                if not handle.done():
                    handle.cancel()
            raise
        return PrepResult(
            ingest=ingest, transcript=results["transcribe"], subtitles=results["detect"]
        )

    async def _ingest(self, spec: LocalizationInput) -> IngestResult:
        return await workflow.execute_activity("ingest", spec, result_type=IngestResult, **INGEST)

    async def _settle(self, handle: workflow.ActivityHandle, restart: Callable, spec):
        """Awaits a stage. When it fails because an artifact it reads is gone, the
        stage that produced that artifact is re-run and the failed stage is run
        again. Each producing stage is re-run at most once per workflow: a second
        loss of the same stage's artifacts fails the workflow."""
        while True:
            try:
                return await handle
            except ActivityError as error:
                stage = _missing_stage(error)
                if stage not in REGENERABLE_STAGES or stage in self._regenerated:
                    raise
                self._regenerated.add(stage)
                await self._ingest(spec)
                handle = restart()
