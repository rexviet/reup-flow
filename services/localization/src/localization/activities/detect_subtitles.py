"""Step 2b: find the burned-in subtitle band with a vision model."""

import asyncio
import math
import statistics
import tempfile
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import extract_frames, probe_video, run_blocking
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
from localization.providers.errors import PermanentProviderError, UnusableAnswer
from localization.storage.object_store import ObjectStore, source_video_key, work_key, work_key_for

SAMPLE_FPS = 2
BATCH_SIZE = 8
FRAME_INTERVAL_MS = 1000 // SAMPLE_FPS
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
# Frames extracted to disk at once: four OCR batches.
CHUNK_FRAMES = 4 * BATCH_SIZE


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
        chunk_frames: int = CHUNK_FRAMES,
        tmp_root: Path | None = None,
    ) -> None:
        self._store = store
        self._ocr = ocr
        self._chunk_frames = chunk_frames
        self._tmp_root = tmp_root

    async def _read(self, image: bytes, scaled: tuple[int, int]) -> tuple[BBox | None, bool]:
        """The detection for one frame, and whether the answer was unusable."""
        try:
            box = await self._ocr.detect_region(image, *scaled)
        except UnusableAnswer:
            return None, True
        return box, False

    @activity.defn(name="detect_subtitles")
    async def detect_subtitles(self, spec: LocalizationInput) -> SubtitleTrack:
        tag_session(spec.aweme_id)
        boxes: list[BBox | None] = []
        unusable: list[int] = []
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
            frames_dir = root / "frames"
            frames_dir.mkdir()
            while True:
                # One chunk of frames on disk at a time, however long the video is.
                frames = await run_blocking(
                    partial(
                        extract_frames,
                        video,
                        frames_dir,
                        start_s=len(boxes) / SAMPLE_FPS,
                        count=self._chunk_frames,
                        fps=SAMPLE_FPS,
                        width=scaled[0],
                        height=scaled[1],
                        # After the first chunk, a seek that yields nothing is the end
                        # of the video, whatever the container's duration claims.
                        at_end=bool(boxes),
                    )
                )
                if not frames:
                    break
                for start in range(0, len(frames), BATCH_SIZE):
                    batch = frames[start : start + BATCH_SIZE]
                    images = [await asyncio.to_thread(p.read_bytes) for p in batch]
                    found = await asyncio.gather(*(self._read(img, scaled) for img in images))
                    unusable += [len(boxes) + i for i, (_, bad) in enumerate(found) if bad]
                    boxes.extend(to_source(box, scaled, source) for box, _ in found)
                    for path in batch:
                        path.unlink()
                    activity.heartbeat(len(boxes))
                if len(frames) < self._chunk_frames:
                    break
            if len(boxes) >= MIN_FRAMES_TO_JUDGE and len(unusable) * 2 > len(boxes):
                raise PermanentProviderError(
                    f"ocr: {len(unusable)} of {len(boxes)} sampled frames had an unusable answer; "
                    "check OCR_MODEL, OCR_BBOX_FORMAT and the provider key"
                )
        region, cues = locate(boxes, info.width, info.height, info.duration_ms)
        document = SubtitleDocument(
            width=info.width,
            height=info.height,
            region=region,
            cues=cues,
            frame_interval_ms=FRAME_INTERVAL_MS,
            frame_boxes=tuple(boxes),
            unusable_frames=tuple(unusable),
        )
        key = work_key_for(spec, 0, "subtitles.json")
        await asyncio.to_thread(put_json, self._store, key, document)
        return SubtitleTrack(subtitle_key=key, region=region)
