"""Step 2b: find the burned-in subtitle band with a vision model."""

import asyncio
import math
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
from localization.storage.object_store import ObjectStore, work_key

SAMPLE_FPS = 2
BATCH_SIZE = 8
FRAME_INTERVAL_MS = 1000 // SAMPLE_FPS
# Bands are compared by the height of their centre, in tenths of the frame.
BAND_TENTHS = 10
# A band seen on fewer frames than this is a stray detection, not a subtitle.
MIN_BAND_FRAMES = 2
# The same band on both sides of a gap shorter than this is one subtitle, not two. A
# line does not vanish for a moment in the middle: a frame without a detection (a
# missed one, or one whose box was unusable) is far more likely. Measured in time, so
# it does not change with the sampling rate.
MAX_GAP_MS = 2000
# The model's boxes are tight, and a little short of the glyphs and their outline. The
# region is padded before it becomes the cover: this fraction of the band's height
# above and below, and this fraction of the frame's width on each side.
REGION_PAD_V = 0.30
REGION_PAD_H = 0.03
# Longest side of a frame sent to the vision model.
OCR_MAX_SIDE = 1024
# Frames extracted to disk at once: four OCR batches.
CHUNK_FRAMES = 4 * BATCH_SIZE


def locate(
    boxes: Sequence[BBox | None], width: int, height: int, duration_ms: int
) -> tuple[BBox | None, tuple[SubtitleCue, ...]]:
    """`boxes[i]` is the detection on the frame sampled at `i * FRAME_INTERVAL_MS`.

    The region is the union of the boxes in the band that appears on the most
    frames, padded (see `pad_region`), so a cover painted over it hides the text on
    every frame. The cues are the runs of frames showing that band, joined across
    gaps of up to `MAX_GAP_MS`."""
    by_band: dict[int, list[int]] = {}
    for index, box in enumerate(boxes):
        if box is not None:
            centre = box.y + box.h / 2
            band = min(BAND_TENTHS - 1, int(centre * BAND_TENTHS / height))
            by_band.setdefault(band, []).append(index)
    if not by_band:
        return None, ()
    frames = max(by_band.values(), key=len)
    if len(frames) < MIN_BAND_FRAMES:
        return None, ()
    members = [boxes[i] for i in frames]
    x1 = min(b.x for b in members if b)
    y1 = min(b.y for b in members if b)
    x2 = max(b.x + b.w for b in members if b)
    y2 = max(b.y + b.h for b in members if b)
    region = pad_region(BBox(x=x1, y=y1, w=x2 - x1, h=y2 - y1), width, height)
    cues: list[SubtitleCue] = []
    start = last = frames[0]
    for index in frames[1:]:
        if (index - last - 1) * FRAME_INTERVAL_MS > MAX_GAP_MS:
            cues.append(_cue(start, last, duration_ms))
            start = index
        last = index
    cues.append(_cue(start, last, duration_ms))
    return region, tuple(cues)


def pad_region(region: BBox, width: int, height: int) -> BBox:
    """`region` with a margin on every side, kept inside the frame."""
    dx = math.ceil(REGION_PAD_H * width)
    dy = math.ceil(REGION_PAD_V * region.h)
    left, top = max(0, region.x - dx), max(0, region.y - dy)
    right, bottom = min(width, region.x + region.w + dx), min(height, region.y + region.h + dy)
    return BBox(x=left, y=top, w=right - left, h=bottom - top)


def _cue(first: int, last: int, duration_ms: int) -> SubtitleCue:
    start = first * FRAME_INTERVAL_MS
    end = (last + 1) * FRAME_INTERVAL_MS
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

    @activity.defn(name="detect_subtitles")
    async def detect_subtitles(self, spec: LocalizationInput) -> SubtitleTrack:
        tag_session(spec.aweme_id)
        boxes: list[BBox | None] = []
        with (
            provider_errors(),
            tempfile.TemporaryDirectory(prefix="subs-", dir=self._tmp_root) as tmp,
        ):
            root = Path(tmp)
            video = root / "source.mp4"
            await run_blocking(download_source, self._store, spec.video_key, video)
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
                    found = await asyncio.gather(
                        *(self._ocr.detect_region(img, *scaled) for img in images)
                    )
                    boxes.extend(to_source(box, scaled, source) for box in found)
                    for path in batch:
                        path.unlink()
                    activity.heartbeat(len(boxes))
                if len(frames) < self._chunk_frames:
                    break
        region, cues = locate(boxes, info.width, info.height, info.duration_ms)
        document = SubtitleDocument(width=info.width, height=info.height, region=region, cues=cues)
        key = work_key(spec.aweme_id, 0, "subtitles.json")
        await asyncio.to_thread(put_json, self._store, key, document)
        return SubtitleTrack(subtitle_key=key, region=region)
