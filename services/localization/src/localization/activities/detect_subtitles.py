"""Step 2b: find the burned-in subtitle band with a vision model."""

import asyncio
import tempfile
from collections.abc import Sequence
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
# One missed frame inside a subtitle does not split it into two cues.
MAX_MISSED_FRAMES = 1


def locate(
    boxes: Sequence[BBox | None], width: int, height: int, duration_ms: int
) -> tuple[BBox | None, tuple[SubtitleCue, ...]]:
    """`boxes[i]` is the detection on the frame sampled at `i * FRAME_INTERVAL_MS`.

    The region is the union of the boxes in the band that appears on the most
    frames, so a cover painted over it hides the text on every frame. The cues
    are the runs of frames showing that band."""
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
    region = BBox(x=x1, y=y1, w=x2 - x1, h=y2 - y1)
    cues: list[SubtitleCue] = []
    start = last = frames[0]
    for index in frames[1:]:
        if index - last > MAX_MISSED_FRAMES + 1:
            cues.append(_cue(start, last, duration_ms))
            start = index
        last = index
    cues.append(_cue(start, last, duration_ms))
    return region, tuple(cues)


def _cue(first: int, last: int, duration_ms: int) -> SubtitleCue:
    start = first * FRAME_INTERVAL_MS
    end = (last + 1) * FRAME_INTERVAL_MS
    if duration_ms:
        end = min(end, duration_ms)
    return SubtitleCue(start_ms=start, end_ms=max(start, end))


class DetectSubtitlesActivity:
    def __init__(self, store: ObjectStore, ocr: OcrProvider) -> None:
        self._store = store
        self._ocr = ocr

    @activity.defn(name="detect_subtitles")
    async def detect_subtitles(self, spec: LocalizationInput) -> SubtitleTrack:
        tag_session(spec.aweme_id)
        boxes: list[BBox | None] = []
        with provider_errors(), tempfile.TemporaryDirectory(prefix="subs-") as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            await run_blocking(download_source, self._store, spec.video_key, video)
            info = await run_blocking(probe_video, video)
            frames_dir = root / "frames"
            frames_dir.mkdir()
            frames = await run_blocking(extract_frames, video, frames_dir, SAMPLE_FPS)
            for start in range(0, len(frames), BATCH_SIZE):
                batch = frames[start : start + BATCH_SIZE]
                pngs = [await asyncio.to_thread(p.read_bytes) for p in batch]
                boxes.extend(
                    await asyncio.gather(
                        *(self._ocr.detect_region(png, info.width, info.height) for png in pngs)
                    )
                )
                for path in batch:
                    path.unlink()
                activity.heartbeat(len(boxes))
        region, cues = locate(boxes, info.width, info.height, info.duration_ms)
        document = SubtitleDocument(width=info.width, height=info.height, region=region, cues=cues)
        key = work_key(spec.aweme_id, 0, "subtitles.json")
        await asyncio.to_thread(put_json, self._store, key, document)
        return SubtitleTrack(subtitle_key=key, region=region)
