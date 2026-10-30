"""The Vbee dubbing workflow: a video dubbed with Vbee's speech-to-text and its cloned
voice, without the GPU services. Deterministic, like `VideoLocalizationWorkflow`: it
decides the order of activities and nothing else, and every read and write of data
happens inside an activity.

    ingest (Demucs)  |  detect_subtitles  |  voice_rate       side by side
    vbee_stt         the vocals, heard in Vietnamese
    plan_slots       the speech cut into slots at its pauses, the words shared out
    shorten          the lines too long for their slot, at the voice's measured rate
    synthesis        every line, in even batches on `tts` (the clip cache is shared)
    rate correction  the rate measured on the clips; lines that overrun at it are
                     shortened once more and spoken again
    track, layout    the dubbed track and the captions
    review           checks in code only; lines that overrun are shortened once more
    render, publish  one render, of the last layout

A separate workflow type: its histories are its own, and `VideoLocalizationWorkflow`
stays as it was. Any change to the ORDER of the commands this workflow issues must be
guarded with `workflow.patched("<name>")`, or a run that is already in flight breaks on
replay.

The run names the Vbee voice with a marker only (`VBEE_CONFIGURED_VOICE`); the activities
that speak put the configured voice in its place, so its id is never in this history.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from localization.agents.temporal import (
    run_slot_shortener,
    slot_shortener_agent,
    slot_shortener_repair_agent,
)
from localization.contracts import (
    DubbedAudio,
    DubDecisions,
    EditSpecRef,
    IngestResult,
    LocalizationInput,
    LocalizedScriptRef,
    PublishRequest,
    PublishResult,
    RenderInput,
    RenderResult,
    ReviewVerdict,
    ScriptResult,
    SubtitleTrack,
    SynthesisPlan,
)
from localization.contracts.vbee_flow import (
    ClipDurations,
    SlotPlan,
    SpeechTranscript,
    VbeeDubbingResult,
)
from localization.text.pieces import split_line
from localization.text.vi_syllables import count_syllables
from localization.workflows.video_localization import (
    ASSEMBLE_AUDIO,
    DETECT_SUBTITLES,
    DUB_IO,
    INGEST,
    IO_QUEUE,
    LAYOUT,
    PUBLISH,
    RENDER,
    REVIEW_ROUNDS,
    STT_QUEUE,
    SYNTHESIZE,
    VOICE_RATE,
    _describe,
    _failed,
    _retry,
    tts_batches,
)

WORKFLOW_NAME = "VbeeDubbingWorkflow"

# Vbee's STT is a remote job polled for up to VBEE_POLL_TIMEOUT_SECONDS (at most an hour),
# after the vocals are uploaded. Workflow code cannot read settings, so the timeout covers
# the largest value the setting allows, plus the upload; the heartbeat (every few seconds
# while the job is polled) notices a dead worker long before it.
VBEE_STT = dict(
    task_queue=STT_QUEUE,
    start_to_close_timeout=timedelta(minutes=70),
    heartbeat_timeout=timedelta(seconds=30),
    # Each attempt is a new job Vbee charges for: a few, for a provider that is down a while.
    retry_policy=_retry(4, 10, 120),
)
# Silence detection over the vocals: an ffmpeg pass of seconds, heartbeating every 10 s.
PLAN_SLOTS = dict(
    task_queue=IO_QUEUE,
    start_to_close_timeout=timedelta(minutes=10),
    heartbeat_timeout=timedelta(seconds=60),
    retry_policy=_retry(3, 5, 60),
)
# The checks of a review: a loudness pass over the dubbed track and arithmetic.
REVIEW_DUB = dict(
    task_queue=IO_QUEUE,
    start_to_close_timeout=timedelta(minutes=10),
    heartbeat_timeout=timedelta(seconds=60),
    retry_policy=_retry(3, 5, 60),
)

# A Vbee clip is a batch request polled until it is done: about 4 s for each piece a line
# is read in (see `text.pieces`), one piece after another. A batch is given five times that
# per piece and a fixed margin; a batch that runs over is retried, and the clips it already
# made are cache hits.
SECONDS_PER_PIECE = 20
SYNTHESIS_MARGIN = timedelta(minutes=2)

# The round of the first pass; a review that sends lines back writes into the next one.
FIRST_ROUND = 0


def synthesis_timeout(texts: list[str]) -> timedelta:
    pieces = sum(max(1, len(split_line(text))) for text in texts)
    return SYNTHESIS_MARGIN + timedelta(seconds=SECONDS_PER_PIECE * pieces)


def measured_rate(plan: SlotPlan, durations: ClipDurations) -> float | None:
    """Syllables a second over every clip spoken: the voice's real rate on these lines.
    None with nothing spoken."""
    texts = {s.id: s.text for s in plan.slots}
    spoken = [(texts[d.id], d.duration_ms) for d in durations.items if d.id in texts]
    total_ms = sum(ms for _, ms in spoken)
    if total_ms <= 0:
        return None
    return sum(count_syllables(text) for text, _ in spoken) / (total_ms / 1000)


def overrun_rate(plan: SlotPlan, durations: ClipDurations, ids: list[int]) -> float | None:
    """The slowest real rate among the lines `ids` whose clip is longer than its slot: the
    rate at which each of them, read as it was, takes at least as long as it did."""
    slots = {s.id: s for s in plan.slots}
    rates = [
        count_syllables(slots[d.id].text) / (d.duration_ms / 1000)
        for d in durations.items
        if d.id in ids and d.id in slots and d.duration_ms > slots[d.id].slot_ms
    ]
    return min(rates) if rates else None


def _texts(plan: SlotPlan) -> list[str]:
    return [s.text for s in plan.slots]


@workflow.defn(name=WORKFLOW_NAME)
class VbeeDubbingWorkflow:
    # Reviews one run may make; the one rerun in between is what a failed review buys.
    _review_rounds = REVIEW_ROUNDS

    # Registers the agents' activities on the worker that hosts this workflow.
    __pydantic_ai_agents__ = [slot_shortener_agent, slot_shortener_repair_agent]

    def __init__(self) -> None:
        # Reviews made so far, and the version of the last script written.
        self._reviews = 0
        self._version = 0

    @workflow.run
    async def run(self, spec: LocalizationInput) -> VbeeDubbingResult:
        """A stage that fails for good is written on the run before the workflow ends, so
        the person who looks finds the reason where the run's outcome is kept."""
        try:
            return await self._pipeline(spec)
        except (ActivityError, ApplicationError) as error:
            await self._record_failure(spec, error)
            raise

    async def _pipeline(self, spec: LocalizationInput) -> VbeeDubbingResult:
        ingest, subtitles, rate, heard = await self._prepare(spec)
        # A video without speech has no slots: nothing is shortened or spoken, and the
        # track is the music alone, as an empty transcript makes it in the other flow.
        plan = await run_slot_shortener(heard, rate)
        script = await self._write(spec, plan, heard, subtitles, FIRST_ROUND)
        await self._synthesize(spec, script, plan)
        plan, script = await self._correct_rate(spec, plan, heard, subtitles, script)
        dub = await self._track(spec, script, ingest, FIRST_ROUND)
        plan, script, dub, published = await self._review_and_publish(
            spec, ingest, subtitles, heard, plan, script, dub
        )
        return VbeeDubbingResult(
            ingest=ingest,
            subtitles=subtitles,
            slots=plan,
            script=script,
            dub=dub,
            publish=published,
        )

    # --- what the dub is made from ----------------------------------------------------

    async def _prepare(
        self, spec: LocalizationInput
    ) -> tuple[IngestResult, SubtitleTrack, float, SlotPlan]:
        """The separation, the source subtitles and the voice's rate start together; the
        speech is sent to Vbee as soon as the separation is done. Whichever fails first
        fails the run and stops the rest: nothing is paid for a run that cannot finish."""
        ingesting = workflow.start_activity("ingest", spec, result_type=IngestResult, **INGEST)
        detecting = workflow.start_activity(
            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES
        )
        rating = workflow.start_activity("voice_rate", spec.voice, result_type=float, **VOICE_RATE)
        handles = [ingesting, detecting, rating]
        try:
            ingest = await self._watching(ingesting, handles)
            hearing = workflow.start_activity(
                "vbee_stt",
                args=[spec, ingest.vocals_key],
                result_type=SpeechTranscript,
                **VBEE_STT,
            )
            handles.append(hearing)
            transcript = await self._watching(hearing, handles)
            heard = await workflow.execute_activity(
                "plan_slots",
                args=[spec, ingest.vocals_key, transcript],
                result_type=SlotPlan,
                **PLAN_SLOTS,
            )
            subtitles = await detecting
            rate = await rating
        except BaseException:
            for handle in handles:
                if not handle.done():
                    handle.cancel()
            raise
        return ingest, subtitles, rate, heard

    @staticmethod
    async def _watching(target: workflow.ActivityHandle, handles: list[workflow.ActivityHandle]):
        """`target`'s result; a sibling that fails first raises at once instead."""
        while not target.done():
            running = [h for h in handles if not h.done()]
            await workflow.wait(running, return_when="FIRST_COMPLETED")
            failed = next((h for h in handles if h is not target and _failed(h)), None)
            if failed is not None:
                await failed
        return await target

    async def _write(
        self,
        spec: LocalizationInput,
        plan: SlotPlan,
        heard: SlotPlan,
        subtitles: SubtitleTrack,
        round_no: int,
    ) -> ScriptResult:
        self._version += 1
        return await workflow.execute_activity(
            "write_slot_script",
            args=[spec, plan, heard, subtitles, self._version, round_no],
            result_type=ScriptResult,
            **DUB_IO,
        )

    async def _synthesize(self, spec: LocalizationInput, script: ScriptResult, plan: SlotPlan):
        """Every distinct line of the script, in even batches; the `tts` queue's limit
        decides how many speak at once. Each batch is given time for the pieces of the
        lines in it."""
        script_key = script.script.script_key
        made = await workflow.execute_activity(
            "plan_synthesis", args=[spec, script_key, None], result_type=SynthesisPlan, **DUB_IO
        )
        hashes = list(made.hashes)
        if not hashes:
            return
        # The plan lists one hash per distinct line, in the order the lines come.
        texts = list(dict.fromkeys(_texts(plan)))
        if len(texts) != len(hashes):
            raise ApplicationError(
                "the synthesis plan does not match the script's lines",
                type="SynthesisPlanMismatch",
                non_retryable=True,
            )
        handles = []
        start = 0
        for batch in tts_batches(hashes):
            batch_texts = texts[start : start + len(batch)]
            start += len(batch)
            handles.append(
                workflow.start_activity(
                    "synthesize_segments",
                    args=[spec, None, script_key, batch],
                    **{**SYNTHESIZE, "start_to_close_timeout": synthesis_timeout(batch_texts)},
                )
            )
        try:
            await workflow.wait(handles, return_when="FIRST_EXCEPTION")
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

    async def _durations(self, spec: LocalizationInput, script: ScriptResult) -> ClipDurations:
        return await workflow.execute_activity(
            "clip_durations",
            args=[spec, script.script.script_key, None],
            result_type=ClipDurations,
            **DUB_IO,
        )

    async def _correct_rate(
        self,
        spec: LocalizationInput,
        plan: SlotPlan,
        heard: SlotPlan,
        subtitles: SubtitleTrack,
        script: ScriptResult,
    ) -> tuple[SlotPlan, ScriptResult]:
        """The voice reads a sample sentence at another rate than it reads real lines. Once
        the lines are spoken, the rate is measured on them; the lines that overrun their
        slot at that rate are shortened at it, once, and spoken again."""
        if not plan.slots:
            return plan, script
        rate = measured_rate(plan, await self._durations(spec, script))
        if rate is None:
            return plan, script
        # Asks nothing when every line fits at the measured rate.
        shorter = await run_slot_shortener(plan, rate)
        if _texts(shorter) == _texts(plan):
            return shorter, script
        script = await self._write(spec, shorter, heard, subtitles, FIRST_ROUND)
        await self._synthesize(spec, script, shorter)
        return shorter, script

    async def _track(
        self, spec: LocalizationInput, script: ScriptResult, ingest: IngestResult, round_no: int
    ) -> DubbedAudio:
        ref = script.script
        segments_key = await workflow.execute_activity(
            "commit_dub",
            args=[spec, ref, None, DubDecisions(items=()), round_no],
            result_type=str,
            **DUB_IO,
        )
        return await workflow.execute_activity(
            "assemble_audio",
            args=[spec, ref, segments_key, ingest, round_no],
            result_type=DubbedAudio,
            **ASSEMBLE_AUDIO,
        )

    # --- review, render and publish ---------------------------------------------------

    @staticmethod
    def _flags(script: ScriptResult) -> list[str]:
        """What stops a run from being published without a person looking."""
        if not script.needs_human:
            return []
        return [
            f"the shortened lines of slots {list(script.flagged_segment_ids)} name a contact "
            "the heard line lacks"
        ]

    async def _review_and_publish(
        self,
        spec: LocalizationInput,
        ingest: IngestResult,
        subtitles: SubtitleTrack,
        heard: SlotPlan,
        plan: SlotPlan,
        script: ScriptResult,
        dub: DubbedAudio,
    ) -> tuple[SlotPlan, ScriptResult, DubbedAudio, PublishResult]:
        round_no = FIRST_ROUND
        flags = self._flags(script)
        verdict: ReviewVerdict | None = None
        stopped: str | None = None
        spec_ref: EditSpecRef | None = None
        for attempt in range(self._review_rounds):
            final = LocalizedScriptRef(script_key=dub.script_key, version=dub.script_version)
            spec_ref = await workflow.execute_activity(
                "layout_captions",
                args=[spec, final, subtitles, ingest, dub, round_no],
                result_type=EditSpecRef,
                **LAYOUT,
            )
            # A review nobody could make leaves the video to a person: it is rendered and
            # kept, never published unreviewed.
            try:
                verdict = await workflow.execute_activity(
                    "review_dub",
                    args=[spec, final, dub, spec_ref, round_no],
                    result_type=ReviewVerdict,
                    **REVIEW_DUB,
                )
            except ActivityError as error:
                stopped = f"the review could not be made: {_describe(error)}"
                break
            self._reviews = attempt + 1
            if verdict.passed or flags or attempt == self._review_rounds - 1:
                break
            shorter, stopped = await self._shorten_overruns(spec, plan, script, verdict)
            # What the shortener noted (a line it could not shorten) is kept either way.
            plan = shorter
            if stopped is not None:
                break
            round_no += 1
            script = await self._write(spec, plan, heard, subtitles, round_no)
            await self._synthesize(spec, script, plan)
            dub = await self._track(spec, script, ingest, round_no)
            flags = self._flags(script)
        assert spec_ref is not None
        # Whatever the verdict, the person or the channel gets the video of the last layout.
        # A render that fails for good fails the run, like any other stage.
        render = await workflow.execute_activity(
            "render",
            RenderInput(aweme_id=spec.aweme_id, spec_key=spec_ref.spec_key),
            result_type=RenderResult,
            **RENDER,
        )
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
        return plan, script, dub, published

    async def _shorten_overruns(
        self,
        spec: LocalizationInput,
        plan: SlotPlan,
        script: ScriptResult,
        verdict: ReviewVerdict,
    ) -> tuple[SlotPlan, str | None]:
        """The plan with exactly the lines the review found too long for their slot
        shortened, at the rate they were really read at, and None; or the plan with nothing
        shortened and why, when there is no such line or none of them could be shortened.
        Anything else the review found (loudness, captions, source subtitle left visible) is
        not fixed by running a step again: it goes to a person."""
        ids = sorted(
            {i.segment_id for i in verdict.issues if i.stage == "dub" and i.segment_id is not None}
        )
        rate = overrun_rate(plan, await self._durations(spec, script), ids) if ids else None
        if rate is None:
            return plan, "nothing the review found is fixed by shortening a line"
        shorter = await run_slot_shortener(plan, rate, ids)
        if _texts(shorter) == _texts(plan):
            return shorter, f"the lines of slots {ids} could not be shortened to fit"
        return shorter, None

    async def _record_failure(
        self, spec: LocalizationInput, error: ActivityError | ApplicationError
    ) -> None:
        try:
            await workflow.execute_activity(
                "publish",
                args=[
                    spec,
                    PublishRequest(
                        outcome="failed", reasons=(_describe(error),), review_rounds=self._reviews
                    ),
                ],
                result_type=PublishResult,
                **PUBLISH,
            )
        except ActivityError:
            # The run is failing for the reason above; not being able to say so must not
            # replace it.
            pass
