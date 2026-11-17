"""Step 2b: find the burned-in subtitle band with a vision model.

A subtitle stays on one row for the whole video, so the row is found from a sparse grid
of frames, not from every frame: a coarse grid first, halved until the row it gives
agrees with a coarser grid's. On 44 approved renders this asked about 3% of the frames
production read at 2 fps and found every region within 1.5% of the frame's height
(`eval/subtitle_sampling_gepa.py`)."""

import asyncio
import math
import statistics
import tempfile
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import extract_frame, probe_video, run_blocking
from localization.activities.ingest import download_source
from localization.activities.persist import put_json
from localization.contracts import (
    BBox,
    LocalizationInput,
    SubtitleCue,
    SubtitleDocument,
    SubtitleTrack,
)
from localization.observability.tracing import tag_session
from localization.providers.base import OcrProvider
from localization.providers.errors import (
    PermanentProviderError,
    TransientProviderError,
    UnusableAnswer,
)
from localization.storage.object_store import ObjectStore, source_video_key, work_key, work_key_for

SAMPLE_FPS = 2
FRAME_INTERVAL_MS = 1000 // SAMPLE_FPS
# Frames of the 2 fps grid between two frames asked on the finest grid: one every 4 s.
FINEST_STEP = 8
# The finest grid holds at least this many frames: a short video is sampled more densely.
MIN_FINEST_FRAMES = 16
# The coarsest grid holds about this many frames.
COARSEST_FRAMES = 8
# A grid settles the region when it found a box on at least this many frames and its
# region's top and bottom are within this fraction of the frame's height of a coarser
# grid's: a third of the 1.5% the cover can be off by unseen.
SETTLED_BOXES = 10
SETTLED_ROWS = 0.005
# Frames read and asked about at once.
DETECT_CONCURRENCY = 16
# Two boxes are the same line of text when they overlap vertically by at least this
# fraction of the shorter one's height.
SAME_LINE_OVERLAP = 0.5
# A band seen on fewer frames than this is a stray detection, not a subtitle.
MIN_BAND_FRAMES = 2
# The same band on both sides of a gap shorter than this is one subtitle, not two. A
# line does not vanish for a moment in the middle: a frame without a detection (a
# missed one, or one whose box was unusable) is far more likely. Measured in time, so
# it does not change with the sampling rate.
MAX_GAP_MS = 2000
# One frame the model answers unusably says nothing about the others; but when more
# than half of the sampled frames (once at least this many were sampled) are
# unusable, the model, its format or its key is wrong, and "no subtitles" would be a
# lie.
MIN_FRAMES_TO_JUDGE = 4
# The consensus region is padded a little: this fraction of its height above and below,
# and this fraction of the frame's width on each side. The model's boxes are right to
# within a few pixels on most frames, so this is a margin, not a correction.
REGION_PAD_V = 0.13
REGION_PAD_H = 0.02
# Burned-in subtitles are centred. A box whose centre is further than this fraction of
# the frame's width from the middle is other text: a disclaimer, a watermark, a name
# card. A disclaimer kept in one corner for most of a video once outvoted the real
# subtitles, which only show while someone speaks.
MAX_CENTRE_OFFSET = 0.15
# Longest side of a frame sent to the vision model.
OCR_MAX_SIDE = 1024
# A frame whose request failed transiently is asked this many times in all, waiting
# `FRAME_RETRY_SECONDS` times the attempt number in between. One 502 in 600 requests
# once threw away every frame read before it.
FRAME_ATTEMPTS = 3
FRAME_RETRY_SECONDS = 2.0


def subtitles_key(aweme_id: str) -> str:
    """Where the subtitle document of a video is stored. It depends on the video alone,
    so a later stage reaches the document without being handed the key."""
    return work_key(aweme_id, 0, "subtitles.json")


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """How much of the shorter of two vertical extents `(top, bottom)` the other covers."""
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    return inter / max(1e-9, min(a[1] - a[0], b[1] - b[0]))


def _rows(box: BBox) -> tuple[float, float]:
    return (box.y, box.y + box.h)


def _consensus(boxes: Sequence[BBox | None]) -> list[int]:
    """The frames that agree on one line of text: the largest group of boxes that
    share a row, with the boxes that do not share the group's median row removed.

    Ties between equally large groups go to the lower one on the screen (a two-line
    subtitle is represented by its bottom line)."""
    seen = [(i, _rows(b)) for i, b in enumerate(boxes) if b is not None]
    best: list[int] = []
    best_key: tuple[int, float] | None = None
    for _, seed in seen:
        group = [i for i, rows in seen if _overlap(rows, seed) >= SAME_LINE_OVERLAP]
        median = _median_rows([boxes[i] for i in group])  # type: ignore[misc]
        inliers = [i for i, rows in seen if _overlap(rows, median) >= SAME_LINE_OVERLAP]
        key = (len(inliers), (median[0] + median[1]) / 2)
        if best_key is None or key > best_key:
            best, best_key = inliers, key
    return best


def _centred(box: BBox, width: int) -> bool:
    return abs(box.x + box.w / 2 - width / 2) <= MAX_CENTRE_OFFSET * width


def _median_rows(members: Sequence[BBox]) -> tuple[float, float]:
    return (
        statistics.median(b.y for b in members),
        statistics.median(b.y + b.h for b in members),
    )


def locate(
    boxes: Sequence[BBox | None],
    width: int,
    height: int,
    duration_ms: int,
    interval_ms: int = FRAME_INTERVAL_MS,
) -> tuple[BBox | None, tuple[SubtitleCue, ...]]:
    """`boxes[i]` is the detection on the frame sampled at `i * interval_ms`.

    The model's box is accurate on most frames and badly off on a few (another text
    elsewhere, a box above the line), so the region comes from a consensus: the
    frames whose boxes share one row (see `_consensus`). Its top and bottom are the
    medians of those boxes, so one off frame cannot stretch it; its width is their
    union, the widest real line. It is padded a little, inside the frame. The cues
    are the runs of those same frames, joined across gaps of up to `MAX_GAP_MS`.
    Boxes that are not centred (see `MAX_CENTRE_OFFSET`) take no part."""
    boxes = [b if b is not None and _centred(b, width) else None for b in boxes]
    frames = _consensus(boxes)
    if len(frames) < MIN_BAND_FRAMES:
        return None, ()
    members = [boxes[i] for i in frames]
    top, bottom = _median_rows(members)  # type: ignore[arg-type]
    x1 = min(b.x for b in members if b)
    x2 = max(b.x + b.w for b in members if b)
    pad_y = math.ceil(REGION_PAD_V * (bottom - top))
    pad_x = math.ceil(REGION_PAD_H * width)
    left, right = max(0, x1 - pad_x), min(width, x2 + pad_x)
    upper, lower = max(0, round(top) - pad_y), min(height, round(bottom) + pad_y)
    region = BBox(x=left, y=upper, w=max(1, right - left), h=max(1, lower - upper))
    cues: list[SubtitleCue] = []
    start = last = frames[0]
    for index in frames[1:]:
        if (index - last - 1) * interval_ms > MAX_GAP_MS:
            cues.append(_cue(start, last, duration_ms, interval_ms))
            start = index
        last = index
    cues.append(_cue(start, last, duration_ms, interval_ms))
    return region, tuple(cues)


def _cue(first: int, last: int, duration_ms: int, interval_ms: int) -> SubtitleCue:
    start = first * interval_ms
    end = (last + 1) * interval_ms
    if duration_ms:
        end = min(end, duration_ms)
    return SubtitleCue(start_ms=start, end_ms=max(start, end))


def grid_steps(frames: int) -> list[int]:
    """The steps, in frames of the 2 fps grid, of the grids a video of `frames` frames
    is sampled on, coarsest first. Each is twice the next, so a finer grid holds every
    frame of the coarser ones."""
    step = max(1, min(FINEST_STEP, frames // MIN_FINEST_FRAMES))
    steps = [step]
    while frames // (steps[-1] * 2) >= COARSEST_FRAMES:
        steps.append(steps[-1] * 2)
    return steps[::-1]


def same_rows(a: BBox, b: BBox, height: int) -> bool:
    """Whether two regions' tops and bottoms agree within `SETTLED_ROWS` of the height."""
    tolerance = SETTLED_ROWS * height
    return abs(a.y - b.y) <= tolerance and abs(a.y + a.h - b.y - b.h) <= tolerance


def ocr_size(width: int, height: int) -> tuple[int, int]:
    """Frames are sent at this size, not the source's: it bounds both the bytes on
    disk and the bytes sent to the vision model."""
    factor = min(1.0, OCR_MAX_SIDE / max(width, height))
    return max(1, round(width * factor)), max(1, round(height * factor))


def to_source(box: BBox | None, scaled: tuple[int, int], source: tuple[int, int]) -> BBox | None:
    """A box in the pixels of the scaled frame, in the pixels of the source."""
    if box is None:
        return None
    fx, fy = source[0] / scaled[0], source[1] / scaled[1]
    x, y = min(round(box.x * fx), source[0] - 1), min(round(box.y * fy), source[1] - 1)
    w = max(1, min(round(box.w * fx), source[0] - x))
    h = max(1, min(round(box.h * fy), source[1] - y))
    return BBox(x=x, y=y, w=w, h=h)


class DetectSubtitlesActivity:
    def __init__(
        self,
        store: ObjectStore,
        ocr: OcrProvider,
        *,
        tmp_root: Path | None = None,
        retry_seconds: float = FRAME_RETRY_SECONDS,
    ) -> None:
        self._store = store
        self._ocr = ocr
        self._retry_seconds = retry_seconds
        self._tmp_root = tmp_root

    async def _read(self, image: bytes, scaled: tuple[int, int]) -> tuple[BBox | None, bool]:
        """The detection for one frame, and whether the answer was unusable. A transient
        failure (a 502 from the gateway, a timeout) is asked again a few times before it
        fails the activity, which would read every frame again from the first."""
        for attempt in range(1, FRAME_ATTEMPTS + 1):
            try:
                return await self._ocr.detect_region(image, *scaled), False
            except UnusableAnswer:
                return None, True
            except TransientProviderError as error:
                if attempt == FRAME_ATTEMPTS:
                    raise
                activity.logger.warning(
                    "frame: %s; asking again (%s of %s)", error, attempt, FRAME_ATTEMPTS - 1
                )
                await asyncio.sleep(self._retry_seconds * attempt)
        raise AssertionError("unreachable")

    @activity.defn(name="detect_subtitles")
    async def detect_subtitles(self, spec: LocalizationInput) -> SubtitleTrack:
        tag_session(spec.aweme_id)
        # What each frame asked about answered, by its index on the 2 fps grid.
        answers: dict[int, BBox | None] = {}
        unusable: set[int] = set()
        # The frames actually asked about.
        read: set[int] = set()
        with (
            provider_errors(),
            tempfile.TemporaryDirectory(prefix="subs-", dir=self._tmp_root) as tmp,
        ):
            root = Path(tmp)
            video = root / "source.mp4"
            await run_blocking(download_source, self._store, source_video_key(spec), video)
            info = await run_blocking(probe_video, video)
            source = (info.width, info.height)
            scaled = ocr_size(*source)
            frames = max(1, math.ceil(info.duration_ms / FRAME_INTERVAL_MS))
            limit = asyncio.Semaphore(DETECT_CONCURRENCY)

            async def frame(index: int) -> bytes | None:
                async with limit:
                    return await run_blocking(
                        partial(
                            extract_frame,
                            video,
                            root / f"frame-{index:06d}.jpg",
                            time_s=index / SAMPLE_FPS,
                            width=scaled[0],
                            height=scaled[1],
                        )
                    )

            async def ask(image: bytes) -> tuple[BBox | None, bool]:
                async with limit:
                    return await self._read(image, scaled)

            seen: list[BBox] = []
            for step in grid_steps(frames):
                new = [i for i in range(0, frames, step) if i not in answers]
                images = await asyncio.gather(*(frame(i) for i in new))
                # A frame the video does not have (past the end of its video stream)
                # shows nothing; it is not asked about.
                shown = [(i, image) for i, image in zip(new, images, strict=True) if image]
                found = await asyncio.gather(*(ask(image) for _, image in shown))
                answers.update(dict.fromkeys(new))
                for (i, _), (box, bad) in zip(shown, found, strict=True):
                    read.add(i)
                    answers[i] = to_source(box, scaled, source)
                    if bad:
                        unusable.add(i)
                activity.heartbeat(len(answers))
                grid = list(range(0, frames, step))
                boxes = [answers[i] for i in grid]
                region, cues = locate(
                    boxes, info.width, info.height, info.duration_ms, FRAME_INTERVAL_MS * step
                )
                hits = sum(box is not None for box in boxes)
                if (
                    region
                    and hits >= SETTLED_BOXES
                    and any(same_rows(region, other, info.height) for other in seen)
                ):
                    break
                if region:
                    seen.append(region)
            if not read:
                raise PermanentProviderError("ffmpeg: the video has no frame to read")
            if len(read) >= MIN_FRAMES_TO_JUDGE and len(unusable) * 2 > len(read):
                raise PermanentProviderError(
                    f"ocr: {len(unusable)} of {len(read)} sampled frames had an unusable answer; "
                    "check OCR_MODEL, OCR_BBOX_FORMAT and the provider key"
                )
        # The document keeps the finest grid sampled: its boxes are what the region and
        # the cues were built from.
        document = SubtitleDocument(
            width=info.width,
            height=info.height,
            duration_ms=info.duration_ms,
            region=region,
            cues=cues,
            frame_interval_ms=FRAME_INTERVAL_MS * step,
            frame_boxes=tuple(boxes),
            unusable_frames=tuple(k for k, i in enumerate(grid) if i in unusable),
        )
        key = work_key_for(spec, 0, "subtitles.json")
        await asyncio.to_thread(put_json, self._store, key, document)
        return SubtitleTrack(subtitle_key=key, region=region)
