"""Step 4b: lay the Vietnamese captions out and write the `EditSpec`.

The captions are the script's text, never a rewrite of it: this module takes the
`LocalizedScript` and decides only where a line breaks, how large it is drawn and
when it is shown. Lines are measured with the same font file the renderer draws
with (`CAPTION_FONT_PATH` on both sides), so a line that fits here fits there.

It never writes or changes text, so it must not reach an agent.
"""

import asyncio
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont
from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.script_document import read_script
from localization.contracts import (
    BBox,
    Caption,
    Cover,
    DubbedAudio,
    DubSegmentsDocument,
    EditSpec,
    EditSpecRef,
    IngestResult,
    LocalizationInput,
    LocalizedScript,
    LocalizedScriptRef,
    SubtitleTrack,
)
from localization.contracts.spec_hash import spec_sha256
from localization.observability.tracing import tag_session
from localization.storage.object_store import ObjectStore, work_key

# Largest first: a caption is drawn as large as it can be and still fit in two lines.
FONT_SIZES = (48, 44, 40, 36, 32)
MAX_LINES = 2
# The cover grows by this fraction of its width and height on each side, so a
# burned-in line that the detector measured a little tight is still hidden.
COVER_GROW = 0.06
# Text stays this far (fraction of the box width) from the box's left and right edge.
BOX_PADDING = 0.04
# Where captions go when the video has no burned-in subtitle: x, y, w, h as
# fractions of the frame.
SAFE_ZONE = (0.05, 0.78, 0.90, 0.14)

_SPACES = re.compile(r"\s+")


# Text is measured at this many times its size and scaled back. A browser places glyphs
# at fractional positions; FreeType at the real size rounds every advance to a whole
# pixel, which adds up to several pixels over a line. At a large size the rounding is
# negligible, so the measurement is the unrounded one.
MEASURE_SCALE = 32
# Ink is measured by drawing the text, so a smaller factor keeps the bitmap small.
INK_SCALE = 8


class FontMeter:
    """Measures text with the caption font."""

    def __init__(self, font_path: str) -> None:
        self.path = font_path
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}
        self._inked: dict[int, ImageFont.FreeTypeFont] = {}

    def font(self, size: int) -> ImageFont.FreeTypeFont:
        """The font at `size` times MEASURE_SCALE."""
        font = self._fonts.get(size)
        if font is None:
            font = self._fonts[size] = ImageFont.truetype(self.path, size * MEASURE_SCALE)
        return font

    def width(self, text: str, size: int) -> float:
        """The advance width of `text` at `size`: what a line takes up."""
        return self.font(size).getlength(text) / MEASURE_SCALE

    def ink_width(self, text: str, size: int) -> float:
        """The width of the pixels the text is made of: narrower than its advance width
        by the space before the first glyph and after the last."""
        font = self._inked.get(size)
        if font is None:
            font = self._inked[size] = ImageFont.truetype(self.path, size * INK_SCALE)
        extent = font.getmask(text).getbbox()
        return 0.0 if extent is None else (extent[2] - extent[0]) / INK_SCALE


@dataclass(frozen=True)
class Fitted:
    lines: tuple[str, ...]
    size: int


def _wrap(words: list[str], meter: FontMeter, size: int, max_width: float) -> list[str] | None:
    """The words as one line if they fit, else the two-line split whose longer line
    is shortest; None when no split fits in `max_width`."""
    whole = " ".join(words)
    if meter.width(whole, size) <= max_width:
        return [whole]
    best: tuple[float, list[str]] | None = None
    for cut in range(1, len(words)):
        first, second = " ".join(words[:cut]), " ".join(words[cut:])
        widest = max(meter.width(first, size), meter.width(second, size))
        if widest <= max_width and (best is None or widest < best[0]):
            best = (widest, [first, second])
    return best[1] if best else None


def fit(text: str, meter: FontMeter, max_width: float) -> Fitted | None:
    """The largest size at which `text` fits in at most two lines, or None."""
    words = text.split()
    if not words:
        return None
    for size in FONT_SIZES:
        lines = _wrap(words, meter, size, max_width)
        if lines is not None:
            return Fitted(tuple(lines), size)
    return None


def split_to_fit(text: str, meter: FontMeter, max_width: float) -> list[Fitted]:
    """`text` as the fewest consecutive pieces that each fit. A single word wider than
    the box cannot be helped by splitting; it is placed alone at the smallest size and
    will overflow the box on both sides."""
    words = text.split()
    pieces: list[Fitted] = []
    index = 0
    while index < len(words):
        end, best = index + 1, fit(words[index], meter, max_width)
        while end < len(words):
            wider = fit(" ".join(words[index : end + 1]), meter, max_width)
            if wider is None:
                break
            end, best = end + 1, wider
        pieces.append(best or Fitted((words[index],), FONT_SIZES[-1]))
        index = end
    return pieces


def _spread(start: int, end: int, weights: list[int]) -> list[tuple[int, int]]:
    """`[start, end)` cut into consecutive spans in proportion to `weights`, with no
    gap and no overlap."""
    total = sum(weights)
    spans, cursor, done = [], start, 0
    for index, weight in enumerate(weights):
        done += weight
        stop = end if index == len(weights) - 1 else start + round((end - start) * done / total)
        spans.append((cursor, stop))
        cursor = stop
    return spans


def grow_cover(region: BBox, frame_w: int, frame_h: int) -> BBox:
    dx, dy = round(region.w * COVER_GROW), round(region.h * COVER_GROW)
    left, top = max(0, region.x - dx), max(0, region.y - dy)
    right, bottom = min(frame_w, region.x + region.w + dx), min(frame_h, region.y + region.h + dy)
    return BBox(x=left, y=top, w=max(1, right - left), h=max(1, bottom - top))


def safe_zone(frame_w: int, frame_h: int) -> BBox:
    fx, fy, fw, fh = SAFE_ZONE
    return BBox(
        x=round(frame_w * fx),
        y=round(frame_h * fy),
        w=round(frame_w * fw),
        h=round(frame_h * fh),
    )


def caption_spans(
    script: LocalizedScript, doc: DubSegmentsDocument, duration_ms: int
) -> list[tuple[int, int, int, str]]:
    """(segment id, start, end, text) for each caption, in time order.

    A caption is shown from its segment's start for as long as its line is spoken or
    its slot lasts, whichever is longer, but never into the next caption and never past
    the end of the video."""
    spoken = {s.id: round(s.duration_ms / s.stretch_ratio) for s in doc.segments}
    ordered = sorted(script.segments, key=lambda s: (s.start_ms, s.id))
    spans = []
    for index, segment in enumerate(ordered):
        text = _SPACES.sub(" ", segment.text_vi).strip()
        if not text or segment.start_ms >= duration_ms:
            continue
        end = max(segment.end_ms, segment.start_ms + spoken.get(segment.id, 0))
        if index + 1 < len(ordered):
            end = min(end, ordered[index + 1].start_ms)
        end = min(end, duration_ms)
        if end > segment.start_ms:
            spans.append((segment.id, segment.start_ms, end, text))
    return spans


def build_captions(
    spans: list[tuple[int, int, int, str]], meter: FontMeter, box: BBox
) -> tuple[Caption, ...]:
    max_width = box.w * (1 - 2 * BOX_PADDING)
    captions: list[Caption] = []
    for segment_id, start, end, text in spans:
        whole = fit(text, meter, max_width)
        pieces = [whole] if whole else split_to_fit(text, meter, max_width)
        weights = [max(1, len(" ".join(p.lines))) for p in pieces]
        for piece, (a, b) in zip(pieces, _spread(start, end, weights), strict=True):
            captions.append(
                Caption(
                    segment_id=segment_id,
                    start_ms=a,
                    end_ms=b,
                    text="\n".join(piece.lines),
                    font_px=piece.size,
                )
            )
    return tuple(captions)


def lay_out(
    *,
    script: LocalizedScript,
    doc: DubSegmentsDocument,
    subtitles: SubtitleTrack,
    ingest: IngestResult,
    dub: DubbedAudio,
    source_key: str,
    meter: FontMeter,
    cover_color: str,
    cover_opacity: float,
) -> EditSpec:
    cover = None
    if subtitles.region is not None:
        cover = Cover(
            region=grow_cover(subtitles.region, ingest.width, ingest.height),
            color=cover_color,
            opacity_pct=round(cover_opacity * 100),
        )
    box = cover.region if cover else safe_zone(ingest.width, ingest.height)
    spans = caption_spans(script, doc, ingest.duration_ms)
    return EditSpec(
        source_key=source_key,
        audio_key=dub.audio_key,
        audio_sha256=dub.audio_sha256,
        script_version=script.version,
        width=ingest.width,
        height=ingest.height,
        fps=ingest.fps,
        duration_ms=ingest.duration_ms,
        cover=cover,
        caption_box=box,
        captions=build_captions(spans, meter, box),
        font=Path(meter.path).name,
    )


def check_one_translation(
    script: LocalizedScript, ref: LocalizedScriptRef, dub: DubbedAudio, doc: DubSegmentsDocument
) -> None:
    """Captions and voice must say the same thing: the track was made from this
    version of the script, and each line it speaks is the line the script holds."""
    if dub.script_version != ref.version or dub.script_key != ref.script_key:
        raise _mismatch("the dubbed audio was made from a different script version")
    if doc.script_version != ref.version or script.version != ref.version:
        raise _mismatch("the dub segments were made from a different script version")
    spoken = {s.id: s.text_vi for s in doc.segments}
    for segment in script.segments:
        if spoken.get(segment.id) != segment.text_vi:
            raise _mismatch(f"segment {segment.id} is spoken differently from how it is written")


def _mismatch(message: str) -> ApplicationError:
    return ApplicationError(message, type="ScriptDubMismatch", non_retryable=True)


@lru_cache(maxsize=4)
def _meter(font_path: str) -> FontMeter:
    return FontMeter(font_path)


class LayoutCaptionsActivity:
    def __init__(
        self,
        store: ObjectStore,
        *,
        font_path: str,
        cover_color: str,
        cover_opacity: float,
    ) -> None:
        self._store = store
        self._font_path = font_path
        self._cover_color = cover_color
        self._cover_opacity = cover_opacity

    @activity.defn(name="layout_captions")
    async def layout_captions(
        self,
        spec: LocalizationInput,
        script: LocalizedScriptRef,
        subtitles: SubtitleTrack,
        ingest: IngestResult,
        dub: DubbedAudio,
        round: int,
    ) -> EditSpecRef:
        tag_session(spec.aweme_id)
        return await asyncio.to_thread(self._build, spec, script, subtitles, ingest, dub, round)

    def _build(
        self,
        spec: LocalizationInput,
        ref: LocalizedScriptRef,
        subtitles: SubtitleTrack,
        ingest: IngestResult,
        dub: DubbedAudio,
        round: int,
    ) -> EditSpecRef:
        script = read_script(self._store, ref.script_key)
        doc = DubSegmentsDocument.model_validate_json(
            self._store.get_bytes(dub.segments_key, produced_by="dub_script")
        )
        check_one_translation(script, ref, dub, doc)
        # The renderer's storage key reaches `work/` only, so the source video it draws
        # is copied there (inside the store, no bytes through this process).
        source_key = work_key(spec.aweme_id, 0, "source.mp4")
        if not self._store.exists(source_key):
            self._store.copy(spec.video_key, source_key, produced_by="ingest")
        edit_spec = lay_out(
            script=script,
            doc=doc,
            subtitles=subtitles,
            ingest=ingest,
            dub=dub,
            source_key=source_key,
            meter=_meter(self._font_path),
            cover_color=self._cover_color,
            cover_opacity=self._cover_opacity,
        )
        spec_key = work_key(spec.aweme_id, round, "edit-spec.json")
        self._store.put_bytes(
            spec_key, edit_spec.model_dump_json().encode(), content_type="application/json"
        )
        return EditSpecRef(
            spec_key=spec_key,
            spec_sha256=spec_sha256(edit_spec),
            script_version=edit_spec.script_version,
            audio_sha256=edit_spec.audio_sha256,
        )
