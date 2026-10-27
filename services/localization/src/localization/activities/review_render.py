"""Step 8: the Reviewer scores the rendered mp4.

The whole review happens inside this one activity, in one of two modes (REVIEWER_MODE):

  jev  checks in code for what can be measured (timing, loudness, caption fit, source
       text left next to the caption, empty or foreign lines) and one call to Jev, a typed
       decision model, for whether each line says what its source says. The only thing
       that leaves the worker is the text of the lines.
  llm  the tool-calling vision agent: frames and OCR are tools it calls in a loop.

Either way the video is a local copy, held for the length of the review and never put in
a Temporal payload, a span or a presigned link: the activity returns the verdict and
nothing else. A retry runs the whole review again, which is fine for a review this short.
"""

import asyncio
import dataclasses
import logging
import shutil
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.assemble_audio import measure_loudness
from localization.activities.ffmpeg import run_blocking, run_ffmpeg
from localization.activities.layout_captions import FontMeter
from localization.activities.script_document import read_script
from localization.agents import review_checks, review_jev
from localization.agents.reviewer import (
    PROMPT_VERSION,
    Frame,
    Loudness,
    ResidualFinding,
    ReviewDeps,
    build_reviewer,
)
from localization.config import DEFAULT_CAPTION_FONT, ReviewerMode
from localization.contracts import (
    DubbedAudio,
    DubSegmentsDocument,
    EditSpec,
    EditSpecRef,
    LocalizationInput,
    LocalizedScript,
    LocalizedScriptRef,
    RenderResult,
    ReviewScores,
    ReviewVerdict,
)
from localization.observability.llm_usage import LlmUsage, Recorder, current_run
from localization.observability.tracing import tag_session
from localization.providers.base import OcrProvider
from localization.providers.errors import UnusableAnswer
from localization.providers.jev import Jev
from localization.storage.object_store import ObjectStore

log = logging.getLogger(__name__)

# What a vision model needs to read a caption, and no more.
FRAME_WIDTH = 768
USAGE = UsageLimits(request_limit=12, tool_calls_limit=24)
HEARTBEAT_SECONDS = 10.0
MAX_ISSUES = 20
# The stage the Jev call is recorded under in `llm_calls`.
JEV_STAGE = "review_jev"
# Each issue takes this much off the score of the dimension it counts against.
SCORE_STEP = 0.25


class LocalVideo:
    """The ports of a review, over a local copy of the rendered mp4."""

    def __init__(
        self,
        video: Path,
        spec: EditSpec,
        ocr: OcrProvider,
        *,
        loudness_target: float,
        true_peak: float,
    ) -> None:
        self._video = video
        self._spec = spec
        self._ocr = ocr
        self._target = loudness_target
        self._peak = true_peak

    def _jpeg_at(self, at_ms: int, width: int) -> bytes:
        out = self._video.parent / f"frame-{at_ms}-{width}.jpg"
        run_ffmpeg(
            [
                "-ss",
                f"{at_ms / 1000:.3f}",
                "-i",
                str(self._video),
                "-frames:v",
                "1",
                "-an",
                "-vf",
                f"scale={width}:-2",
                "-q:v",
                "4",
                str(out),
            ],
            timeout=120,
        )
        try:
            return out.read_bytes()
        finally:
            out.unlink(missing_ok=True)

    async def frames(self, times_ms: Sequence[int]) -> list[Frame]:
        width = min(FRAME_WIDTH, self._spec.width)
        return [
            Frame(t, await run_blocking(self._jpeg_at, t, width, heartbeat_every=HEARTBEAT_SECONDS))
            for t in times_ms
        ]

    async def loudness(self) -> Loudness:
        measured = await run_blocking(
            measure_loudness, self._video, self._target, self._peak, heartbeat_every=10.0
        )
        return Loudness(float(measured["input_i"]), float(measured["input_tp"]))

    async def find_text(self, times_ms: Sequence[int]) -> list[ResidualFinding]:
        # The OCR sees the frame at the video's own size, so the box it returns is in the
        # same pixels as the spec's cover.
        found = []
        for t in times_ms:
            jpeg = await run_blocking(self._jpeg_at, t, self._spec.width)
            try:
                box = await self._ocr.detect_region(jpeg, self._spec.width, self._spec.height)
            except UnusableAnswer:
                # One frame the OCR cannot read must not end a review that has the frames
                # themselves to go on: the Reviewer is told this one was not checked.
                found.append(ResidualFinding(t, None, unavailable=True))
                continue
            found.append(ResidualFinding(t, None if box is None else (box.x, box.y, box.w, box.h)))
        return found


async def _heartbeat_while(task: asyncio.Future) -> None:
    while not task.done():
        await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
        activity.heartbeat()


def _score(findings: Sequence[review_checks.Finding], dimension: str) -> float:
    count = sum(f.dimension == dimension for f in findings)
    return max(0.0, 1.0 - SCORE_STEP * count)


class ReviewRenderActivity:
    def __init__(
        self,
        store: ObjectStore,
        ocr: OcrProvider,
        model: Callable[[], Model],
        *,
        loudness_lufs: float,
        true_peak_dbtp: float,
        mode: ReviewerMode = "llm",
        jev: Callable[[], Jev] | None = None,
        recorder: Recorder | None = None,
        font_path: str = str(DEFAULT_CAPTION_FONT),
    ) -> None:
        if mode == "jev" and jev is None:
            raise ValueError("the jev review needs a Jev client")
        self._store = store
        self._ocr = ocr
        self._model = model
        self._target = loudness_lufs
        self._peak = true_peak_dbtp
        self._mode = mode
        self._jev = jev
        self._recorder = recorder
        self._meter = FontMeter(font_path)

    @activity.defn(name="review_render")
    async def review_render(
        self,
        spec: LocalizationInput,
        render: RenderResult,
        script: LocalizedScriptRef,
        dub: DubbedAudio,
        spec_ref: EditSpecRef,
        round: int,
    ) -> ReviewVerdict:
        tag_session(spec.aweme_id)
        script_doc = await asyncio.to_thread(read_script, self._store, script.script_key)
        segments = DubSegmentsDocument.model_validate_json(
            await asyncio.to_thread(
                self._store.get_bytes, dub.segments_key, produced_by="dub_script"
            )
        )
        edit_spec = EditSpec.model_validate_json(
            await asyncio.to_thread(
                self._store.get_bytes, spec_ref.spec_key, produced_by="captions"
            )
        )
        root = Path(tempfile.mkdtemp(prefix="review-"))
        try:
            video = root / "render.mp4"
            await asyncio.to_thread(
                self._store.download_file, render.render_key, video, produced_by="render"
            )
            ports = LocalVideo(
                video,
                edit_spec,
                self._ocr,
                loudness_target=self._target,
                true_peak=self._peak,
            )
            if self._mode == "jev":
                return await self._review_with_jev(script_doc, segments, edit_spec, ports)
            deps = ReviewDeps(script=script_doc, segments=segments, spec=edit_spec, ports=ports)
            agent = build_reviewer(self._model())
            run = asyncio.ensure_future(
                agent.run(
                    f"Review the dubbed video ({PROMPT_VERSION}, round {round}).",
                    deps=deps,
                    usage_limits=USAGE,
                )
            )
            beat = asyncio.ensure_future(_heartbeat_while(run))
            try:
                result = await run
            except UsageLimitExceeded as exc:
                raise ApplicationError(
                    f"the Reviewer did not settle within its limits: {exc}",
                    type="ReviewRunaway",
                    non_retryable=True,
                ) from None
            finally:
                run.cancel()
                beat.cancel()
                await asyncio.gather(run, beat, return_exceptions=True)
            return result.output
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def _record(self, usage: LlmUsage) -> None:
        run = current_run()
        if self._recorder is not None and run is not None:
            await self._recorder(usage, dataclasses.replace(run, stage=JEV_STAGE))

    async def _review_with_jev(
        self,
        script: LocalizedScript,
        segments: DubSegmentsDocument,
        spec: EditSpec,
        ports: LocalVideo,
    ) -> ReviewVerdict:
        assert self._jev is not None
        work = asyncio.ensure_future(self._judge(script, segments, spec, ports, self._jev()))
        beat = asyncio.ensure_future(_heartbeat_while(work))
        try:
            return await work
        finally:
            work.cancel()
            beat.cancel()
            await asyncio.gather(work, beat, return_exceptions=True)

    async def _judge(
        self,
        script: LocalizedScript,
        segments: DubSegmentsDocument,
        spec: EditSpec,
        ports: LocalVideo,
        jev: Jev,
    ) -> ReviewVerdict:
        found = review_checks.line_findings(script.segments)
        found += review_checks.timing_findings(
            script.segments,
            segments,
            spec.duration_ms,
            await ports.loudness(),
            target_lufs=self._target,
            true_peak_dbtp=self._peak,
        )
        found += await asyncio.to_thread(review_checks.caption_findings, spec, self._meter)
        times = review_checks.residual_times(spec)
        if times:
            found += review_checks.residual_findings(spec, await ports.find_text(times))
        # Lines that failed the cheap checks are already issues: Jev is asked about the rest.
        broken = {f.issue.segment_id for f in found if f.dimension == "fidelity"}
        asked = [s for s in script.segments if s.id not in broken]
        for part in review_jev.chunks(asked):
            result = await jev.ask(
                review_jev.build_state(part), review_jev.build_questions(part), self._record
            )
            verdicts = review_jev.fidelity_issues(part, result.answers)
            found += [review_checks.Finding("fidelity", i) for i in verdicts.issues]
            log.info(
                "jev review: %d line(s), worst %.2f, %d ms, %d+%d tokens",
                len(part),
                verdicts.worst,
                result.latency_ms,
                result.input_tokens,
                result.output_tokens,
            )
        issues = tuple(f.issue for f in found)[:MAX_ISSUES]
        return ReviewVerdict(
            passed=not issues,
            scores=ReviewScores(
                fidelity=_score(found, "fidelity"),
                timing=_score(found, "timing"),
                visual=_score(found, "visual"),
            ),
            issues=issues,
        )
