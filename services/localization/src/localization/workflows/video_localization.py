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
    dubber_repair_agent,
    dubber_single_agent,
    localizer,
    localizer_repair_agent,
    localizer_single_agent,
    run_dubber,
    run_localizer,
)
from localization.contracts import (
    DubbedAudio,
    DubbedScriptRef,
    DubDecision,
    DubDecisions,
    EditSpecRef,
    FitReport,
    IngestResult,
    LocalizationInput,
    LocalizationResult,
    LocalizedScriptRef,
    PrepResult,
    ProjectRef,
    PublishRequest,
    PublishResult,
    RenderInput,
    RenderResult,
    ReviewVerdict,
    ScriptResult,
    SegmentRooms,
    SourceSnapshot,
    SubtitleTrack,
    SynthesisPlan,
    Transcript,
    VoiceProfile,
)
from localization.contracts.models import PublishOutcome, VoiceCheck
from localization.workflows.review_routing import (
    IssueKey,
    NoRoute,
    Route,
    issue_key,
    plan_rerun,
    route_issues,
)

# Task queues. `workflows` carries only this workflow; every activity runs elsewhere.
MEDIA_QUEUE = "media"
STT_QUEUE = "stt"
IO_QUEUE = "io"
TTS_QUEUE = "tts"
LLM_QUEUE = "llm"
RENDER_QUEUE = "render"
# The voice check: quick, and never behind a separation on `media` or a subtitle detection
# on `io`, since every run starts with it.
VOICE_CHECK_QUEUE = "voice_check"

# Marks a run that has the Localizer stage. A run recorded before the stage existed
# replays without this marker and finishes where it used to; a new run records it.
LOCALIZE_PATCH = "localize-script"
# Marks a run that has the dubbing stage (synthesis, the Dubber, the dubbed track).
DUB_PATCH = "dub-audio"
# Marks a run that has the layout, render, review and publish steps. A run recorded
# before them ended after the dubbing and replays to that end.
REVIEW_PATCH = "review-publish"
# Marks a run that asks, before another round, whether a rerun could fix what the review
# found. A run recorded before that went straight to the rerun.
UNFIXABLE_PATCH = "stop-unfixable-review"
# Marks a run that measures the voice's speaking rate while the video is being prepared. A
# run recorded before measured it after the preparation, just before the Localizer.
VOICE_RATE_PATCH = "voice-rate-with-ingest"
# Marks a run that splits the texts to synthesize into even batches, at least one per
# concurrent `tts` slot. A run recorded before cut them into runs of `TTS_BATCH` and a rest.
EVEN_TTS_BATCHES_PATCH = "even-tts-batches"
# Marks a run whose rounds are reviewed before the video is rendered, and which renders once,
# after the last round. A run recorded before rendered every round and reviewed the render.
REVIEW_BEFORE_RENDER_PATCH = "review-before-render"
# Marks a run that asks, once the video is prepared, whether it is a product advertisement,
# and ends there when it is. A run recorded before went from the preparation straight to
# the Localizer.
AD_GATE_PATCH = "skip-advertisements"
# Marks a run that first checks the source and ends there when there is nothing to localize:
# a file without a video stream is skipped, a video without a voice has its original
# published as it is. A run recorded before began with the ingest.
VOICE_CHECK_PATCH = "publish-originals-without-voice"
REVIEW_PROJECT_PATCH = "durable-review-project"
CONTEXTUAL_LOCALIZER_PATCH = "contextual-localizer-unresolved"

# Reviews of one run: the first looks at the first layout, the second at the one made after
# the one rerun a failed review sends the run back for.
REVIEW_ROUNDS = 2

# Hashes sent to the provider in one activity. Small enough that a killed worker
# loses little, large enough that the activity overhead is not the cost.
TTS_BATCH = 10
# Batches the `tts` queue speaks at once: the default `tts_concurrency`. Lines inside a
# batch are spoken one after another, so a script is split into at least this many
# batches of even size, or one slot would sit idle while the other speaks them all.
TTS_PARALLEL = 2

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

LAYOUT = dict(
    task_queue=MEDIA_QUEUE,
    start_to_close_timeout=timedelta(minutes=10),
    retry_policy=_retry(3, 5, 60),
)
# The longest render the renderer allows itself is 35 minutes (see its deadline); it
# heartbeats every few seconds, so a worker that died is noticed in a minute.
RENDER = dict(
    task_queue=RENDER_QUEUE,
    start_to_close_timeout=timedelta(hours=2),
    heartbeat_timeout=timedelta(seconds=60),
    retry_policy=RetryPolicy(
        maximum_attempts=3,
        initial_interval=timedelta(seconds=10),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(minutes=2),
        # The renderer marks its own permanent refusals; a lost artifact is permanent too.
        non_retryable_error_types=_PERMANENT,
    ),
)
REVIEW = dict(
    task_queue=LLM_QUEUE,
    start_to_close_timeout=timedelta(minutes=10),
    heartbeat_timeout=timedelta(seconds=60),
    retry_policy=RetryPolicy(
        maximum_attempts=2,
        initial_interval=timedelta(seconds=10),
        non_retryable_error_types=[*_PERMANENT, "ReviewRunaway"],
    ),
)
# One Jev call on the transcript's text, each attempt bounded by the provider's own
# timeout. Retried like the review; a video that could not be classified is localized.
CLASSIFY = dict(
    task_queue=LLM_QUEUE,
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy=RetryPolicy(
        maximum_attempts=2,
        initial_interval=timedelta(seconds=10),
        non_retryable_error_types=_PERMANENT,
    ),
)
# A download, a decode and a few seconds of the detector. A video that could not be checked
# is localized.
VOICE_CHECK = dict(
    task_queue=VOICE_CHECK_QUEUE,
    start_to_close_timeout=timedelta(minutes=10),
    heartbeat_timeout=timedelta(minutes=2),
    retry_policy=_retry(3, 5, 60),
)
PUBLISH = dict(
    task_queue=IO_QUEUE,
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy=RetryPolicy(
        maximum_attempts=5,
        initial_interval=timedelta(seconds=2),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=30),
        non_retryable_error_types=[*_PERMANENT, "PublishRefused"],
    ),
)

# The round every dubbing artifact of a run's first pass belongs to. A rerun after a
# review writes into the next round's folder.
DUB_ROUND = 0


def tts_batches(hashes: list[str]) -> list[list[str]]:
    """`hashes` in order, cut into the fewest batches of at most `TTS_BATCH` that still
    give each of the `TTS_PARALLEL` slots one, their sizes differing by at most one."""
    n = len(hashes)
    count = max(-(-n // TTS_BATCH), min(TTS_PARALLEL, n))
    if count == 0:
        return []
    size, extra = divmod(n, count)
    batches: list[list[str]] = []
    start = 0
    for i in range(count):
        end = start + size + (1 if i < extra else 0)
        batches.append(hashes[start:end])
        start = end
    return batches


# `asyncio.FIRST_EXCEPTION`, spelled out because workflow code does not import asyncio.
_FIRST_EXCEPTION = "FIRST_EXCEPTION"
_FIRST_COMPLETED = "FIRST_COMPLETED"


def _failed(handle: workflow.ActivityHandle) -> bool:
    return handle.done() and not handle.cancelled() and handle.exception() is not None


def _missing_stage(error: ActivityError) -> str | None:
    cause = error.cause
    if isinstance(cause, ApplicationError) and cause.type == ARTIFACT_MISSING and cause.details:
        stage = cause.details[0]
        return stage if isinstance(stage, str) else None
    return None


def _describe(error: ActivityError | ApplicationError) -> str:
    """`Type: message` of what actually failed, for a person to read."""
    cause = error.cause if isinstance(error, ActivityError) else error
    kind = (
        cause.type if isinstance(cause, ApplicationError) and cause.type else type(cause).__name__
    )
    return f"{kind}: {cause}"[:400]


@workflow.defn(name="VideoLocalizationWorkflow")
class VideoLocalizationWorkflow:
    # Reviews one run may make; the one rerun in between is what a failed review buys.
    _review_rounds = REVIEW_ROUNDS

    # Registers the agents' activities on the worker that hosts this workflow.
    __pydantic_ai_agents__ = [
        localizer,
        dubber,
        localizer_single_agent,
        localizer_repair_agent,
        dubber_single_agent,
        dubber_repair_agent,
    ]

    def __init__(self) -> None:
        self._project = False
        self._contextual = False
        self._regenerated: set[str] = set()
        # What the dubbing stage needs from the Localizer stage, kept so a script
        # that is lost and made again replaces the one the stage works from.
        self._rate: float = 0.0
        # True when the rate was measured during the preparation (and is in `_rate`).
        self._rate_measured = False
        # The speech engine the voice reported, and what the Dubber decided about each
        # line it was given: a rerun of the dubbing for some lines keeps the others'.
        self._engine: str | None = None
        self._decisions: dict[int, DubDecision] = {}
        # Lines whose Dubber rewrite was refused, in the script the track was built from.
        self._refused: tuple[int, ...] = ()
        # Reviews made so far in this run.
        self._reviews = 0
        # What an earlier rerun was asked to fix: the same problem again is not one more
        # rerun would fix.
        self._asked: set[IssueKey] = set()

    @workflow.run
    async def run(self, spec: LocalizationInput) -> LocalizationResult:
        """A stage that fails for good is written on the run before the workflow ends, so
        the person who looks finds the reason where the run's outcome is kept."""
        try:
            return await self._pipeline(spec)
        except (ActivityError, ApplicationError) as error:
            # An activity that failed, or an agent that gave up (it raises in the workflow
            # itself, as an ApplicationError). A run recorded before the steps that write
            # the row existed has no marker, so it replays without the extra activity.
            if workflow.patched(REVIEW_PATCH):
                await self._record_failure(spec, error)
            raise

    async def _pipeline(self, spec: LocalizationInput) -> LocalizationResult:
        self._contextual = spec.delivery_mode == "review_project" and workflow.patched(
            CONTEXTUAL_LOCALIZER_PATCH
        )
        if self._checks_for_voice():
            outcome = await self._unprepared_end(spec)
            if outcome is not None:
                return LocalizationResult(script=None, publish=await self._end(spec, outcome))
        self._project = self._delivers_project(spec)
        if self._project:
            snapshot = await workflow.execute_activity(
                "capture_review_source", spec, result_type=SourceSnapshot, **ASSEMBLE_SCRIPT
            )
            spec = spec.model_copy(update={"source_snapshot": snapshot})
        prep = await self._prepare(spec)
        if self._asks_about_ads() and await self._is_advertisement(spec, prep):
            return LocalizationResult(prep=prep, script=None, publish=await self._skip_ad(spec))
        if not workflow.patched(LOCALIZE_PATCH):
            return LocalizationResult(prep=prep, script=None)
        prep, script = await self._localize(spec, prep)
        if not workflow.patched(DUB_PATCH):
            return LocalizationResult(prep=prep, script=script)
        # The stage may have made the script (and the preparation) again: the result
        # reports the ones the track was built from, with their review flag.
        prep, script, dub = await self._dub_with_regeneration(spec, prep, script)
        if not workflow.patched(REVIEW_PATCH):
            return LocalizationResult(prep=prep, script=script, dub=dub)
        script, dub, published = await self._review_and_publish(spec, prep, script, dub)
        return LocalizationResult(prep=prep, script=script, dub=dub, publish=published)

    def _delivers_project(self, spec: LocalizationInput) -> bool:
        return (
            spec.delivery_mode == "review_project"
            and spec.voice.provider == "voicestudio"
            and workflow.patched(REVIEW_PROJECT_PATCH)
        )

    def _checks_for_voice(self) -> bool:
        """True for a run that checks the source for a voice before preparing it."""
        return workflow.patched(VOICE_CHECK_PATCH)

    async def _unprepared_end(self, spec: LocalizationInput) -> PublishOutcome | None:
        """How the run ends before anything is prepared, or None to go on. A file without a
        video stream is skipped, whatever its audio holds: it is pictures with music laid
        over them, and the ingest would only refuse it after the download. A video without a
        voice has its original published as it is. A check that cannot be made (an
        unreadable file, the retries spent) lets the video through: the check saves work, and
        must never cost a video that is worth localizing."""
        try:
            check = await workflow.execute_activity(
                "check_voice", spec, result_type=VoiceCheck, **VOICE_CHECK
            )
        except ActivityError as error:
            workflow.logger.warning("voice not checked, localized as usual: %s", _describe(error))
            return None
        if not check.has_video:
            return "skipped_no_video"
        return "published_original" if check.no_voice else None

    async def _end(self, spec: LocalizationInput, outcome: PublishOutcome) -> PublishResult:
        """Leaves the end on the run: the original published, or the file skipped. No render,
        no review."""
        return await workflow.execute_activity(
            "publish",
            args=[spec, PublishRequest(outcome=outcome)],
            result_type=PublishResult,
            **PUBLISH,
        )

    def _asks_about_ads(self) -> bool:
        """True for a run that asks, once prepared, whether the video is an advertisement."""
        return workflow.patched(AD_GATE_PATCH)

    async def _is_advertisement(self, spec: LocalizationInput, prep: PrepResult) -> bool:
        """Whether the video's speech sells a product. A question that cannot be answered
        (Jev down, its answers unusable, the retries spent) lets the video through: the
        check saves work, and must never cost a video that is worth localizing."""
        try:
            return await workflow.execute_activity(
                "classify_video", args=[spec, prep.transcript], result_type=bool, **CLASSIFY
            )
        except ActivityError as error:
            workflow.logger.warning("not classified, localized as usual: %s", _describe(error))
            return False

    async def _skip_ad(self, spec: LocalizationInput) -> PublishResult:
        """Leaves the skipped advertisement on the run: no render, no review."""
        return await workflow.execute_activity(
            "publish",
            args=[spec, PublishRequest(outcome="skipped_ad")],
            result_type=PublishResult,
            **PUBLISH,
        )

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
        if not self._rate_measured:
            self._rate = await workflow.execute_activity(
                "voice_rate", spec.voice, result_type=float, **VOICE_RATE
            )
        rate = self._rate
        transcript_key = prep.transcript.transcript_key
        subtitle_key = prep.subtitles.subtitle_key
        lines = await run_localizer(
            LocalizerDeps(
                aweme_id=spec.aweme_id,
                workflow_id=workflow.info().workflow_id,
                channel_id=spec.channel_id,
                transcript_key=transcript_key,
                subtitle_key=subtitle_key,
                source_video_key=(
                    spec.source_snapshot.snapshot_key if spec.source_snapshot else None
                ),
                syllables_per_sec=rate,
                segment_count=prep.transcript.segment_count,
                contextual=self._contextual,
            )
        )
        return await workflow.execute_activity(
            "assemble_script",
            args=[spec, transcript_key, subtitle_key, lines, 0, 1],
            result_type=ScriptResult,
            **ASSEMBLE_SCRIPT,
        )

    def _start_rate(self, spec: LocalizationInput) -> workflow.ActivityHandle:
        return workflow.start_activity("voice_rate", spec.voice, result_type=float, **VOICE_RATE)

    async def _ingest_beside_rate(
        self, spec: LocalizationInput
    ) -> tuple[IngestResult, workflow.ActivityHandle]:
        """Ingest and the voice's rate measured at the same time. Whichever fails first
        fails the run and stops the other: nothing is paid for a run that cannot finish."""
        ingesting = workflow.start_activity("ingest", spec, result_type=IngestResult, **INGEST)
        rating = self._start_rate(spec)
        try:
            # The preparation goes on as soon as the ingest is done, the rate still running.
            while not ingesting.done():
                running = [h for h in (ingesting, rating) if not h.done()]
                await workflow.wait(running, return_when=_FIRST_COMPLETED)
                if _failed(rating):
                    await rating
            return await ingesting, rating
        except BaseException:
            for handle in (ingesting, rating):
                if not handle.done():
                    handle.cancel()
            raise

    async def _prepare(self, spec: LocalizationInput) -> PrepResult:
        # The voice does not depend on the video: its rate is measured while the video is
        # ingested, and settled with the stages after it.
        rating: workflow.ActivityHandle | None = None
        if workflow.patched(VOICE_RATE_PATCH):
            ingest, rating = await self._ingest_beside_rate(spec)
        else:
            ingest = await self._ingest(spec)
        # 2a and 2b are independent; both are started before either is awaited.
        transcribing = workflow.start_activity(
            "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE
        )
        detecting = workflow.start_activity(
            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES
        )
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
        if rating is not None:
            stages["rate"] = (rating, lambda: self._start_rate(spec))
        try:
            await workflow.wait([h for h, _ in stages.values()], return_when=_FIRST_EXCEPTION)
            # A stage that already failed is settled first, so a failure is acted on as
            # soon as it happens instead of after the slower sibling finishes.
            order = sorted(stages, key=lambda n: not _failed(stages[n][0]))
            results = {}
            for name in order:
                handle, restart = stages[name]
                results[name] = await self._settle(handle, restart, spec)
        except BaseException:
            # The sibling's result can no longer be used: stop paying for it.
            for handle, _ in stages.values():
                if not handle.done():
                    handle.cancel()
            raise
        if rating is not None:
            self._rate = results["rate"]
            self._rate_measured = True
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
        self, spec: LocalizationInput, prep: PrepResult, script: ScriptResult, round_no: int = 0
    ) -> DubbedAudio:
        profile = await workflow.execute_activity(
            "voice_profile", spec.voice, result_type=VoiceProfile, **VOICE_PROFILE
        )
        engine = profile.engine
        self._engine = engine
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
        # Always the full list: with fewer arguments than the activity has parameters the
        # SDK does not convert any of them to their types.
        in_round = [round_no]
        written = await workflow.execute_activity(
            "write_dubbed_script",
            args=[spec, first, decisions, *in_round],
            result_type=DubbedScriptRef,
            **DUB_IO,
        )
        self._refused = written.refused_ids
        self._decisions = {d.id: d for d in decisions.items}
        final = LocalizedScriptRef(script_key=written.script_key, version=written.version)
        await self._synthesize(spec, engine, final.script_key)
        segments_key = await workflow.execute_activity(
            "commit_dub",
            args=[spec, final, engine, decisions, *in_round],
            result_type=str,
            **DUB_IO,
        )
        return await workflow.execute_activity(
            "assemble_audio",
            args=[spec, final, segments_key, prep.ingest, DUB_ROUND + round_no],
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
        if workflow.patched(EVEN_TTS_BATCHES_PATCH):
            batches = tts_batches(hashes)
        else:
            batches = [hashes[i : i + TTS_BATCH] for i in range(0, len(hashes), TTS_BATCH)]
        handles = [
            workflow.start_activity(
                "synthesize_segments",
                args=[spec, engine, script_key, batch],
                **SYNTHESIZE,
            )
            for batch in batches
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

    # --- layout, render, review and publish ----------------------------------------------

    async def _review_and_publish(
        self, spec: LocalizationInput, prep: PrepResult, script: ScriptResult, dub: DubbedAudio
    ) -> tuple[ScriptResult, DubbedAudio, PublishResult]:
        self._reviews = 0
        if self._project:
            return script, dub, await self._deliver_project(spec, prep, script, dub)
        return await self._rounds(spec, prep, script, dub)

    async def _deliver_project(self, spec, prep, script, dub) -> PublishResult:
        final = LocalizedScriptRef(script_key=dub.script_key, version=dub.script_version)
        layout = await workflow.execute_activity(
            "layout_captions",
            args=[spec, final, prep.subtitles, prep.ingest, dub, 0],
            result_type=EditSpecRef,
            **LAYOUT,
        )
        reasons = self._flags(script)
        verdict = None
        try:
            verdict = await workflow.execute_activity(
                "review_before_render",
                args=[spec, final, dub, layout, 0],
                result_type=ReviewVerdict | None,
                **REVIEW,
            )
        except ActivityError as error:
            reasons.append(f"the review could not be made: {_describe(error)}")
        if verdict is None:
            reasons.append("data-only reviewer unavailable; manual review required")
        else:
            self._reviews = 1
            if not verdict.passed or verdict.issues:
                reasons.extend(f"{i.stage}: {i.reason}" for i in verdict.issues)
                if not verdict.issues:
                    reasons.append("the automatic review did not pass")
        project = await workflow.execute_activity(
            "build_review_project",
            args=[spec, dub, prep.ingest, prep.subtitles, self._engine],
            result_type=ProjectRef,
            task_queue=IO_QUEUE,
            start_to_close_timeout=timedelta(minutes=45),
            retry_policy=_retry(3, 10, 120),
        )
        return await workflow.execute_activity(
            "publish",
            args=[
                spec,
                PublishRequest(
                    outcome="needs_human" if reasons else "ready_for_review",
                    project_key=project.project_key,
                    reasons=tuple(r[:500] for r in reasons[:20]),
                    review_rounds=self._reviews,
                    verdict=verdict,
                    dub=dub,
                    safety_flag=bool(self._flags(script)),
                ),
            ],
            result_type=PublishResult,
            **PUBLISH,
        )

    async def _record_failure(
        self, spec: LocalizationInput, error: ActivityError | ApplicationError
    ) -> None:
        reason = _describe(error)
        try:
            await workflow.execute_activity(
                "publish",
                args=[
                    spec,
                    PublishRequest(
                        outcome="failed", reasons=(reason,), review_rounds=self._reviews
                    ),
                ],
                result_type=PublishResult,
                **PUBLISH,
            )
        except ActivityError:
            # The run is failing for the reason above; not being able to say so must not
            # replace it.
            pass

    def _flags(self, script: ScriptResult) -> list[str]:
        """What stops a run from being published without a person looking."""
        flags = []
        if script.needs_human and (script.flagged_segment_ids or not self._contextual):
            ids = list(script.flagged_segment_ids)
            flags.append(
                f"the Localizer flagged segments {ids}: a line has a contact its source lacks"
            )
        if self._contextual:
            if script.inferred_segment_ids:
                flags.append(
                    f"inferred segments require review: {list(script.inferred_segment_ids)}"
                )
            if script.unresolved_segment_ids:
                flags.append(
                    "unresolved speech intentionally omitted: "
                    f"{list(script.unresolved_segment_ids)}"
                )
        if self._refused:
            flags.append(
                f"the Dubber's rewrite of segments {list(self._refused)} was refused "
                "(it named a contact the source lacks)"
            )
        return flags

    def _review_first(self) -> bool:
        """True for a run whose rounds are reviewed before the video is rendered."""
        return workflow.patched(REVIEW_BEFORE_RENDER_PATCH)

    async def _render(self, spec: LocalizationInput, spec_ref: EditSpecRef) -> RenderResult:
        return await workflow.execute_activity(
            "render",
            RenderInput(aweme_id=spec.aweme_id, spec_key=spec_ref.spec_key),
            result_type=RenderResult,
            **RENDER,
        )

    async def _rounds(
        self, spec: LocalizationInput, prep: PrepResult, script: ScriptResult, dub: DubbedAudio
    ) -> tuple[ScriptResult, DubbedAudio, PublishResult]:
        # The review reads what the render is made from, so a round is not rendered to be
        # reviewed: the video is rendered once, after the last round. A run recorded before
        # rendered each round and reviewed the render.
        review_first = self._review_first()
        round_no = 0
        flags = self._flags(script)
        verdict: ReviewVerdict | None = None
        render: RenderResult | None = None
        stopped: str | None = None
        earlier_spec: str | None = None
        for attempt in range(self._review_rounds):
            # The track and the captions are made from one script: the one the track was
            # built from, which is the Dubber's, not the Localizer's.
            final = LocalizedScriptRef(script_key=dub.script_key, version=dub.script_version)
            spec_ref = await workflow.execute_activity(
                "layout_captions",
                args=[spec, final, prep.subtitles, prep.ingest, dub, round_no],
                result_type=EditSpecRef,
                **LAYOUT,
            )
            if attempt and spec_ref.spec_sha256 == earlier_spec:
                # Whatever the rerun did, the viewer would get the same video: another
                # review would only repeat the last verdict.
                stopped = "the rerun produced the same video, so the reviewer's problems remain"
                break
            # A review nobody could make (the model answered nothing usable, the provider
            # was down, the OCR failed) leaves the video to a person: it is rendered and
            # kept, never published unreviewed and never thrown away.
            found: ReviewVerdict | None = None
            if review_first:
                try:
                    found = await workflow.execute_activity(
                        "review_before_render",
                        args=[spec, final, dub, spec_ref, round_no],
                        # A type hint the payload converter reads; the stub asks for a class.
                        result_type=ReviewVerdict | None,  # type: ignore[arg-type]
                        **REVIEW,
                    )
                except ActivityError as error:
                    stopped = f"the review could not be made: {_describe(error)}"
                    break
            if found is None:
                # A reviewer that looks at frames (or a run recorded before the review
                # came first) is shown the render of this round.
                render = await self._render(spec, spec_ref)
                try:
                    found = await workflow.execute_activity(
                        "review_render",
                        args=[spec, render, final, dub, spec_ref, round_no],
                        result_type=ReviewVerdict,
                        **REVIEW,
                    )
                except ActivityError as error:
                    stopped = f"the review could not be made: {_describe(error)}"
                    break
            verdict = found
            self._reviews = attempt + 1
            if verdict.passed or flags:
                # A run that needs a person goes to one with the verdict in hand; sending
                # it round again would only spend more before they look.
                break
            if attempt == self._review_rounds - 1:
                break
            route = await self._route(spec, prep, dub, verdict)
            if isinstance(route, NoRoute):
                stopped = route.reason
                break
            earlier_spec = spec_ref.spec_sha256
            round_no += 1
            script, dub = await self._rerun(spec, prep, script, dub, route, round_no)
            flags = self._flags(script)
        if review_first and (render is None or render.spec_sha256 != spec_ref.spec_sha256):
            # Whatever the verdict, the person or the channel gets the video of the last
            # layout. A render that fails for good fails the run, like any other stage.
            render = await self._render(spec, spec_ref)
        # Before the review came first, the first pass of the loop always rendered: only the
        # later ones could stop before they did. A review that could not be made leaves
        # `verdict` empty.
        assert render is not None
        reasons = list(flags)
        if verdict is not None and not verdict.passed:
            summary = "; ".join(f"{i.stage}: {i.reason}" for i in verdict.issues)[:400]
            reasons.append(f"the review did not pass after {self._reviews} round(s): {summary}")
        if stopped is not None:
            reasons.append(stopped)
        ok = verdict is not None and verdict.passed and not reasons
        published = await workflow.execute_activity(
            "publish",
            args=[
                spec,
                PublishRequest(
                    outcome="published" if ok else "needs_human",
                    render_key=render.render_key,
                    reasons=tuple(r[:500] for r in reasons[:20]),
                    review_rounds=self._reviews,
                    verdict=verdict,
                    dub=dub,
                    safety_flag=bool(flags),
                ),
            ],
            result_type=PublishResult,
            **PUBLISH,
        )
        return script, dub, published

    async def _route(
        self, spec: LocalizationInput, prep: PrepResult, dub: DubbedAudio, verdict: ReviewVerdict
    ) -> Route | NoRoute:
        """Where a failed review sends the run: back to a stage that can fix what it found,
        or to a person when nothing it found can be fixed by running a stage again."""
        if not workflow.patched(UNFIXABLE_PATCH):
            return route_issues(verdict.issues)
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
        route = plan_rerun(verdict.issues, rooms.items, frozenset(self._asked))
        if isinstance(route, Route):
            self._asked |= {issue_key(i) for i in verdict.issues}
        return route

    async def _rerun(
        self,
        spec: LocalizationInput,
        prep: PrepResult,
        script: ScriptResult,
        dub: DubbedAudio,
        route: Route,
        round_no: int,
    ) -> tuple[ScriptResult, DubbedAudio]:
        """Runs again the stage the review named and what depends on it, into the
        folder of `round_no`. Transcript, subtitles, ingest and the clips already
        spoken are not touched."""
        if route.rerun == "localize":
            lines = await run_localizer(
                LocalizerDeps(
                    aweme_id=spec.aweme_id,
                    workflow_id=workflow.info().workflow_id,
                    channel_id=spec.channel_id,
                    transcript_key=prep.transcript.transcript_key,
                    subtitle_key=prep.subtitles.subtitle_key,
                    source_video_key=(
                        spec.source_snapshot.snapshot_key if spec.source_snapshot else None
                    ),
                    syllables_per_sec=self._rate,
                    segment_count=prep.transcript.segment_count,
                    contextual=self._contextual,
                ),
                route.notes,
            )
            fresh = await workflow.execute_activity(
                "assemble_script",
                args=[
                    spec,
                    prep.transcript.transcript_key,
                    prep.subtitles.subtitle_key,
                    lines,
                    round_no,
                    dub.script_version + 1,
                ],
                result_type=ScriptResult,
                **ASSEMBLE_SCRIPT,
            )
            return fresh, await self._dub(spec, prep, fresh, round_no)
        if route.rerun == "dub":
            return script, await self._redub(spec, prep, dub, route, round_no)
        return script, dub

    async def _redub(
        self,
        spec: LocalizationInput,
        prep: PrepResult,
        dub: DubbedAudio,
        route: Route,
        round_no: int,
    ) -> DubbedAudio:
        """The Dubber again on the lines the review named; the other lines keep their
        text, their clips and what the Dubber decided about them."""
        current = LocalizedScriptRef(script_key=dub.script_key, version=dub.script_version)
        decisions = await run_dubber(
            DubberDeps(
                aweme_id=spec.aweme_id,
                script_key=current.script_key,
                voice=spec.voice,
                engine=self._engine,
                syllables_per_sec=self._rate,
                pending_ids=route.segment_ids,
            ),
            route.notes,
        )
        written = await workflow.execute_activity(
            "write_dubbed_script",
            args=[spec, current, decisions, round_no],
            result_type=DubbedScriptRef,
            **DUB_IO,
        )
        self._refused = written.refused_ids
        # What was decided before still holds for the lines the review did not name.
        self._decisions = {**self._decisions, **{d.id: d for d in decisions.items}}
        merged = DubDecisions(items=tuple(self._decisions[i] for i in sorted(self._decisions)))
        final = LocalizedScriptRef(script_key=written.script_key, version=written.version)
        await self._synthesize(spec, self._engine, final.script_key)
        segments_key = await workflow.execute_activity(
            "commit_dub",
            args=[spec, final, self._engine, merged, round_no],
            result_type=str,
            **DUB_IO,
        )
        return await workflow.execute_activity(
            "assemble_audio",
            args=[spec, final, segments_key, prep.ingest, DUB_ROUND + round_no],
            result_type=DubbedAudio,
            **ASSEMBLE_AUDIO,
        )
