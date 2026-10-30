"""Captions as images, for the render to lay over the video.

Each caption is drawn once, as the browser composition used to draw it: the lines exactly
as the layout split them ("\\n", never rewrapped), each centred on its own, the block
centred in the caption box, `font_px` with a line height of 1.25, white, with the CSS
shadow `0 2px 6px rgba(0, 0, 0, 0.75)`. Line widths come from the layout's own
`FontMeter`, so a line is centred on the width it was fitted with.

The captions are then turned into one image stream: one picture per stretch of frames in
which the same captions are shown, all of one size and position, with a transparent picture
where none is. ffmpeg reads it through the concat demuxer and lays it over the video with
a single overlay, however many captions there are.
"""

import math
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from localization.activities.layout_captions import LINE_HEIGHT, FontMeter
from localization.contracts import BBox, Caption

# Text is drawn at this many times its size and reduced, so glyphs land at fractional
# positions and are smoothed the way the browser smoothed them.
SUPERSAMPLE = 4
SHADOW_OFFSET_Y = 2
# The CSS blur radius; the Gaussian it stands for has half of it as standard deviation.
SHADOW_BLUR = 6
SHADOW_ALPHA = 0.75


class Cancelled(Exception):
    """The render was asked to stop."""


@dataclass(frozen=True)
class Placed:
    """An RGBA picture of one caption and where its top-left corner goes in the frame."""

    image: Image.Image
    x: int
    y: int


class CaptionPainter:
    def __init__(self, font_path: str, meter: FontMeter | None = None) -> None:
        self._path = font_path
        self._meter = meter or FontMeter(font_path)
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        font = self._fonts.get(size)
        if font is None:
            font = self._fonts[size] = ImageFont.truetype(self._path, size * SUPERSAMPLE)
        return font

    def paint(self, caption: Caption, box: BBox) -> Placed:
        px = caption.font_px
        lines = caption.text.split("\n")
        widths = [self._meter.width(line, px) for line in lines]
        block_w = max(widths)
        line_h = LINE_HEIGHT * px
        block_h = line_h * len(lines)
        # The block is centred in the box and overflows it evenly when it is wider.
        left = box.x + (box.w - block_w) / 2
        top = box.y + (box.h - block_h) / 2
        # Room for ink that leaves the line box (stacked Vietnamese marks, overhanging
        # glyphs) and for the shadow.
        margin = math.ceil(px / 2 + SHADOW_OFFSET_Y + SHADOW_BLUR)
        x0, y0 = math.floor(left) - margin, math.floor(top) - margin
        width = math.ceil(left + block_w) + margin - x0
        height = math.ceil(top + block_h) + margin - y0

        font = self._font(px)
        ascent, descent = font.getmetrics()
        big = Image.new("L", (width * SUPERSAMPLE, height * SUPERSAMPLE), 0)
        draw = ImageDraw.Draw(big)
        for index, (line, line_w) in enumerate(zip(lines, widths, strict=True)):
            if not line:
                continue
            x = (left + (block_w - line_w) / 2 - x0) * SUPERSAMPLE
            line_top = (top + index * line_h - y0) * SUPERSAMPLE
            # The glyphs' ascent and descent are centred in the line box, as CSS does.
            baseline = line_top + (line_h * SUPERSAMPLE - (ascent + descent)) / 2 + ascent
            draw.text((x, baseline), line, font=font, fill=255, anchor="ls")
        text = big.reduce(SUPERSAMPLE)

        shadow = Image.new("L", text.size, 0)
        shadow.paste(text, (0, SHADOW_OFFSET_Y))
        shadow = shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR / 2))
        shadow = shadow.point(lambda v: round(v * SHADOW_ALPHA))
        image = Image.new("RGBA", text.size, (0, 0, 0, 0))
        image.putalpha(shadow)
        white = Image.new("RGBA", text.size, (255, 255, 255, 0))
        white.putalpha(text)
        return Placed(Image.alpha_composite(image, white), x0, y0)


def frame_at(ms: int, fps: int) -> int:
    """The frame a time falls on, half a frame rounding up (as `Math.round` did)."""
    return math.floor(ms * fps / 1000 + 0.5)


def frame_count(duration_ms: int, fps: int) -> int:
    """Frames in the video. A partial last frame is still drawn."""
    return max(1, math.ceil(duration_ms * fps / 1000))


def caption_frames(caption: Caption, fps: int) -> tuple[int, int]:
    """[first, end) frames a caption is shown on: at least one."""
    first = frame_at(caption.start_ms, fps)
    return first, first + max(1, frame_at(caption.end_ms, fps) - first)


@dataclass(frozen=True)
class CaptionTrack:
    """The concat list of caption pictures, and where its pictures go in the frame."""

    playlist: Path
    x: int
    y: int


def _spans(captions: tuple[Caption, ...], fps: int, frames: int) -> list[tuple[int, int, tuple]]:
    """(first frame, end frame, indexes of the captions shown) for each stretch of frames
    in which the same captions are shown, covering [0, frames) with no gap. Later captions
    are drawn over earlier ones, as later sequences were."""
    shown = [caption_frames(c, fps) for c in captions]
    cuts = sorted({0, frames, *(min(f, frames) for pair in shown for f in pair)})
    spans = []
    for first, end in zip(cuts, cuts[1:], strict=False):
        if first >= end:
            continue
        active = tuple(i for i, (a, b) in enumerate(shown) if a <= first < b)
        if spans and spans[-1][2] == active:
            spans[-1] = (spans[-1][0], end, active)
        else:
            spans.append((first, end, active))
    return spans


def _microseconds(frame: int, fps: int) -> int:
    """When a picture starting at `frame` starts: half a frame early, so the frame's own
    timestamp is never a rounding error before it."""
    return 0 if frame == 0 else round((frame - 0.5) * 1_000_000 / fps)


def caption_track(
    captions: tuple[Caption, ...],
    box: BBox,
    *,
    fps: int,
    frames: int,
    painter: CaptionPainter,
    work_dir: Path,
    cancel: threading.Event | None = None,
) -> CaptionTrack | None:
    """Writes the caption pictures and their concat list into `work_dir`; None when no
    caption is ever on screen."""
    spans = _spans(captions, fps, frames)
    if not any(active for _, _, active in spans):
        return None
    needed = sorted({i for _, _, active in spans for i in active})
    placed: dict[int, Placed] = {}
    for index in needed:
        if cancel is not None and cancel.is_set():
            raise Cancelled
        placed[index] = painter.paint(captions[index], box)
    x = min(p.x for p in placed.values())
    y = min(p.y for p in placed.values())
    width = max(p.x + p.image.width for p in placed.values()) - x
    height = max(p.y + p.image.height for p in placed.values()) - y

    names: dict[tuple, str] = {}
    blank = "blank.png"
    Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(work_dir / blank, compress_level=1)
    entries: list[str] = ["ffconcat version 1.0"]
    for first, end, active in spans:
        name = blank
        if active:
            name = names.get(active, "")
            if not name:
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                for index in active:
                    p = placed[index]
                    canvas.alpha_composite(p.image, (p.x - x, p.y - y))
                name = names[active] = f"caption-{len(names):05d}.png"
                canvas.save(work_dir / name, compress_level=1)
        seconds = (_microseconds(end, fps) - _microseconds(first, fps)) / 1_000_000
        entries += [f"file {name}", f"duration {seconds:.6f}"]
    # The demuxer ignores the length of its last entry; a closing one makes it count.
    entries.append(f"file {blank}")
    playlist = work_dir / "captions.ffconcat"
    playlist.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return CaptionTrack(playlist, x, y)
