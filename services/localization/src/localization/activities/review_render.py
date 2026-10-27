"""Step 8: the Reviewer scores the rendered mp4.

The whole review happens inside this one activity. The frames the Reviewer looks at
are cut from a local copy of the video, held in memory for the length of the review
and never put in a Temporal payload, a span or a presigned link: the activity returns
the verdict and nothing else. A retry runs the whole review again, which is fine for
a review this short.
"""

import asyncio
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
from localization.activities.script_document import read_script
from localization.agents.reviewer import (
    PROMPT_VERSION,
    Frame,
    Loudness,
    ResidualFinding,
    ReviewDeps,
    build_reviewer,
)
from localization.contracts import (
    DubbedAudio,
    DubSegmentsDocument,
    EditSpec,
    EditSpecRef,
    LocalizationInput,
    LocalizedScriptRef,
    RenderResult,
    ReviewVerdict,
)
from localization.observability.tracing import tag_session
from localization.providers.base import OcrProvider
from localization.storage.object_store import ObjectStore

# What a vision model needs to read a caption, and no more.
FRAME_WIDTH = 768
USAGE = UsageLimits(request_limit=12, tool_calls_limit=24)
HEARTBEAT_SECONDS = 10.0


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
            box = await self._ocr.detect_region(jpeg, self._spec.width, self._spec.height)
            found.append(ResidualFinding(t, None if box is None else (box.x, box.y, box.w, box.h)))
        return found


async def _heartbeat_while(task: asyncio.Future) -> None:
    while not task.done():
        await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
        activity.heartbeat()


class ReviewRenderActivity:
    def __init__(
        self,
        store: ObjectStore,
        ocr: OcrProvider,
        model: Callable[[], Model],
        *,
        loudness_lufs: float,
        true_peak_dbtp: float,
    ) -> None:
        self._store = store
        self._ocr = ocr
        self._model = model
        self._target = loudness_lufs
        self._peak = true_peak_dbtp

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
