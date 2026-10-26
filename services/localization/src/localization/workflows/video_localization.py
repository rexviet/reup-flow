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

from localization.agents.temporal import (
    DubberDeps,
    LocalizerDeps,
    dubber,
    localizer,
    run_dubber,
    run_localizer,
)
from localization.contracts import (
    DubbedAudio,
    DubDecisions,
    FitReport,
    IngestResult,
    LocalizationInput,
    LocalizationResult,
    LocalizedScriptRef,
    PrepResult,
    ScriptResult,
    SubtitleTrack,
    SynthesisPlan,
    Transcript,
    VoiceProfile,
)

# Task queues. `workflows` carries only this workflow; every activity runs elsewhere.
MEDIA_QUEUE = "media"
STT_QUEUE = "stt"
IO_QUEUE = "io"
TTS_QUEUE = "tts"

# Marks a run that has the Localizer stage. A run recorded before the stage existed
# replays without this marker and finishes where it used to; a new run records it.
LOCALIZE_PATCH = "localize-script"
# Marks a run that has the dubbing stage (synthesis, the Dubber, the dubbed track).
DUB_PATCH = "dub-audio"

# Hashes sent to the provider in one activity. Small enough that a killed worker
# loses little, large enough that the activity overhead is not the cost.
TTS_BATCH = 10

ARTIFACT_MISSING = "ArtifactMissingError"
# The stages whose artifacts the workflow can regenerate (once) when one is lost.
REGENERABLE_STAGES = frozenset({"ingest"})
# The same for the dubbing stage: it also reads the script (written by the Localizer,
# then by its own script step) and the music stem.
DUB_REGENERABLE_STAGES = frozenset({"ingest", "localize", "dub_script"})
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
VOICE_PROFILE = dict(
    task_queue=IO_QUEUE,
    start_to_close_timeout=timedelta(minutes=1),
    retry_policy=_retry(5, 2, 30),
)
DUB_IO = dict(
    task_queue=IO_QUEUE,
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy=_retry(5, 5, 60),
)
SYNTHESIZE = dict(
    task_queue=TTS_QUEUE,
    start_to_close_timeout=timedelta(minutes=30),
    # The activity heartbeats every few seconds while a provider call is in flight,
    # so a worker that died is noticed in seconds, not after the full timeout.
    heartbeat_timeout=timedelta(seconds=20),
    retry_policy=_retry(8, 5, 300),
)
ASSEMBLE_AUDIO = dict(
    task_queue=MEDIA_QUEUE,
    start_to_close_timeout=timedelta(minutes=20),
    heartbeat_timeout=timedelta(minutes=2),
    retry_policy=_retry(3, 10, 120),
)

# The round every dubbing artifact of a run's first pass belongs to.
DUB_ROUND = 0


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
    # Registers the agents' activities on the worker that hosts this workflow.
    __pydantic_ai_agents__ = [localizer, dubber]

    def __init__(self) -> None:
        self._regenerated: set[str] = set()
        # What the dubbing stage needs from the Localizer stage, kept so a script
        # that is lost and made again replaces the one the stage works from.
        self._rate: float = 0.0

    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        prep = await self._prepare(spec)
        if not workflow.patched(LOCALIZE_PATCH):
            return LocalizationResult(prep=prep, script=None)
        prep, script = await self._localize(spec, prep)
        if not workflow.patched(DUB_PATCH):
            return LocalizationResult(prep=prep, script=script)
        # The stage may have made the script (and the preparation) again: the result
        # reports the ones the track was built from, with their review flag.
        prep, script, dub = await self._dub_with_regeneration(spec, prep, script)
        return LocalizationResult(prep=prep, script=script, dub=dub)

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
        self._rate = rate
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

    async def _dub_with_regeneration(
        self, spec: LocalizationInput, prep: PrepResult, script: ScriptResult
    ) -> tuple[PrepResult, ScriptResult, DubbedAudio]:
        """The dubbing stage. An artifact it reads that has gone missing is made
        again by the stage that produced it, once, and the stage then runs on. Returns
        the preparation and script the track was built from, which differ from the
        ones passed in when the Localizer ran again."""
        while True:
            try:
                return prep, script, await self._dub(spec, prep, script)
            except ActivityError as error:
                stage = _missing_stage(error)
                if stage not in DUB_REGENERABLE_STAGES or stage in self._regenerated:
                    raise
                self._regenerated.add(stage)
                if stage == "ingest":
                    await self._ingest(spec)
                elif stage == "localize":
                    prep, script = await self._localize(spec, prep)

    async def _dub(
        self, spec: LocalizationInput, prep: PrepResult, script: ScriptResult
    ) -> DubbedAudio:
        profile = await workflow.execute_activity(
            "voice_profile", spec.voice, result_type=VoiceProfile, **VOICE_PROFILE
        )
        engine = profile.engine
        first = script.script
        await self._synthesize(spec, engine, first.script_key)
        fit = await workflow.execute_activity(
            "measure_fit",
            args=[spec, first.script_key, engine],
            result_type=FitReport,
            **DUB_IO,
        )
        decisions = DubDecisions(items=())
        if fit.unfit_ids:
            decisions = await run_dubber(
                DubberDeps(
                    aweme_id=spec.aweme_id,
                    script_key=first.script_key,
                    voice=spec.voice,
                    engine=engine,
                    syllables_per_sec=self._rate,
                    pending_ids=fit.unfit_ids,
                )
            )
        # The script after the Dubber is the one every later stage reads. Lines the
        # Dubber rewrote have no clip yet; the second synthesis pass makes exactly
        # those (the rest are cache hits).
        final = await workflow.execute_activity(
            "write_dubbed_script",
            args=[spec, first, decisions],
            result_type=LocalizedScriptRef,
            **DUB_IO,
        )
        await self._synthesize(spec, engine, final.script_key)
        segments_key = await workflow.execute_activity(
            "commit_dub",
            args=[spec, final, engine, decisions],
            result_type=str,
            **DUB_IO,
        )
        return await workflow.execute_activity(
            "assemble_audio",
            args=[spec, final, segments_key, prep.ingest, DUB_ROUND],
            result_type=DubbedAudio,
            **ASSEMBLE_AUDIO,
        )

    async def _synthesize(
        self, spec: LocalizationInput, engine: str | None, script_key: str
    ) -> None:
        """Every distinct text of the script, in batches. The queue's concurrency
        limit decides how many batches speak at once."""
        plan = await workflow.execute_activity(
            "plan_synthesis",
            args=[spec, script_key, engine],
            result_type=SynthesisPlan,
            **DUB_IO,
        )
        hashes = list(plan.hashes)
        if not hashes:
            return
        handles = [
            workflow.start_activity(
                "synthesize_segments",
                args=[spec, engine, script_key, hashes[i : i + TTS_BATCH]],
                **SYNTHESIZE,
            )
            for i in range(0, len(hashes), TTS_BATCH)
        ]
        try:
            await workflow.wait(handles, return_when=_FIRST_EXCEPTION)
            # A batch that already failed is acted on first, not after the ones
            # listed before it have finished.
            failed = next((h for h in handles if _failed(h)), None)
            if failed is not None:
                await failed
            for handle in handles:
                await handle
        except BaseException:
            # A batch that failed makes the rest pointless to pay for.
            for handle in handles:
                if not handle.done():
                    handle.cancel()
            raise
