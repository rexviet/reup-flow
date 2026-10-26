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

from localization.agents.temporal import LocalizerDeps, localizer, run_localizer
from localization.contracts import (
    IngestResult,
    LocalizationInput,
    LocalizationResult,
    PrepResult,
    ScriptResult,
    SubtitleTrack,
    Transcript,
)

# Task queues. `workflows` carries only this workflow; every activity runs elsewhere.
MEDIA_QUEUE = "media"
STT_QUEUE = "stt"
IO_QUEUE = "io"
TTS_QUEUE = "tts"

# Marks a run that has the Localizer stage. A run recorded before the stage existed
# replays without this marker and finishes where it used to; a new run records it.
LOCALIZE_PATCH = "localize-script"

ARTIFACT_MISSING = "ArtifactMissingError"
# The stages whose artifacts the workflow can regenerate (once) when one is lost.
REGENERABLE_STAGES = frozenset({"ingest"})
# The preparation stages whose artifacts the Localizer reads; if one is gone by then,
# that stage is run again (once) and the Localizer with it.
LOCALIZER_INPUT_STAGES = frozenset({"transcribe", "detect_subtitles"})

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
VOICE_RATE = dict(
    # The voice reads one sample sentence the first time it is used; the TTS queue's
    # concurrency limit covers it like any other synthesis.
    task_queue=TTS_QUEUE,
    start_to_close_timeout=timedelta(minutes=10),
    retry_policy=_retry(8, 5, 300),
)
ASSEMBLE_SCRIPT = dict(
    task_queue=IO_QUEUE,
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy=_retry(5, 5, 60),
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
    # Registers the Localizer's activities on the worker that hosts this workflow.
    __pydantic_ai_agents__ = [localizer]

    def __init__(self) -> None:
        self._regenerated: set[str] = set()

    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        prep = await self._prepare(spec)
        if not workflow.patched(LOCALIZE_PATCH):
            return LocalizationResult(prep=prep, script=None)
        prep, script = await self._localize(spec, prep)
        return LocalizationResult(prep=prep, script=script)

    async def _localize(
        self, spec: LocalizationInput, prep: PrepResult
    ) -> tuple[PrepResult, ScriptResult]:
        """The Localizer stage. A transcript or subtitle document that has gone
        missing is made again by the stage that produced it, once, and the stage
        then runs on the new one; a second loss fails the workflow."""
        while True:
            try:
                return prep, await self._localize_once(spec, prep)
            except ActivityError as error:
                stage = _missing_stage(error)
                if stage not in LOCALIZER_INPUT_STAGES or stage in self._regenerated:
                    raise
                self._regenerated.add(stage)
                prep = await self._rerun_input_stage(stage, spec, prep)

    async def _rerun_input_stage(
        self, stage: str, spec: LocalizationInput, prep: PrepResult
    ) -> PrepResult:
        if stage == "transcribe":

            def start() -> workflow.ActivityHandle:
                return workflow.start_activity(
                    "transcribe",
                    args=[spec, prep.ingest.vocals_key],
                    result_type=Transcript,
                    **TRANSCRIBE,
                )

            transcript = await self._settle(start(), start, spec)
            return prep.model_copy(update={"transcript": transcript})

        def start_detect() -> workflow.ActivityHandle:
            return workflow.start_activity(
                "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES
            )

        subtitles = await self._settle(start_detect(), start_detect, spec)
        return prep.model_copy(update={"subtitles": subtitles})

    async def _localize_once(self, spec: LocalizationInput, prep: PrepResult) -> ScriptResult:
        rate = await workflow.execute_activity(
            "voice_rate", spec.voice, result_type=float, **VOICE_RATE
        )
        transcript_key = prep.transcript.transcript_key
        subtitle_key = prep.subtitles.subtitle_key
        lines = await run_localizer(
            LocalizerDeps(
                aweme_id=spec.aweme_id,
                workflow_id=workflow.info().workflow_id,
                channel_id=spec.channel_id,
                transcript_key=transcript_key,
                subtitle_key=subtitle_key,
                syllables_per_sec=rate,
                segment_count=prep.transcript.segment_count,
            )
        )
        return await workflow.execute_activity(
            "assemble_script",
            args=[spec, transcript_key, subtitle_key, lines],
            result_type=ScriptResult,
            **ASSEMBLE_SCRIPT,
        )

    async def _prepare(self, spec: LocalizationInput) -> PrepResult:
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
