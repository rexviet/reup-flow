"""Fits a detected subtitle box to the text itself.

The vision model's box is imprecise from frame to frame: too tall, too wide, shifted,
sometimes clipping the glyphs. A cover painted from it is a blur of those errors, and
the union over a whole video keeps the worst of them. The text is measured instead:
burned-in subtitles are overwhelmingly light glyphs with a dark outline, so the light
pixels near the model's box give the text's own bounding box.
"""

import io
import math

from PIL import Image, ImageChops

from localization.contracts import BBox

# A pixel is glyph when its darkest channel is at least this bright.
LIGHT_MIN = 215
# The model's box is searched this much beyond itself (fractions of its height and
# width), so a box that is shifted or clips the text still finds all of it.
EXPAND_V = 0.30
EXPAND_H = 0.05
# With too few light pixels (coloured subtitles, a bright background) the box is kept,
# padded by this fraction of its height on every side. Never a large pad.
FALLBACK_PAD = 0.10
# Fewest light pixels, as a fraction of the searched area, that make a text band.
MIN_LIGHT_FRACTION = 0.004
MIN_LIGHT_PIXELS = 30
# A row is part of the text when this many of its pixels are light, at least: low, so
# the thin tops of diacritics and the tails of descenders are not cut off.
ROW_MIN_PIXELS = 2
# Rows of the same line may be separated by this many dark rows.
ROW_GAP = 2
# A column, within the text's rows, is part of it at this fraction of the text height.
COL_MIN_FRACTION = 0.05
# Light columns closer than this many text heights are one line; farther groups
# smaller than the share below are specks (a lamp, a sign's edge), not subtitle.
COL_GAP_HEIGHTS = 1.0
SPECK_SHARE = 0.05
# Marks that belong to a line but sit apart from it (the dots under Vietnamese vowels,
# thin tails): a separate group of rows closer than this fraction of the line's height
# is part of the line. A bright bar farther away is not.
TAIL_GAP_FRACTION = 0.25
# The dark outline around the glyphs, which the light threshold does not see.
OUTLINE_FRACTION = 0.12
OUTLINE_MIN_PIXELS = 3


def fallback(box: BBox, width: int, height: int) -> BBox:
    pad = math.ceil(FALLBACK_PAD * box.h)
    return _clamped(
        box.x - pad, box.y - pad, box.x + box.w + pad, box.y + box.h + pad, width, height
    )


def _clamped(left: int, top: int, right: int, bottom: int, width: int, height: int) -> BBox:
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, max(right, left + 1)), min(height, max(bottom, top + 1))
    return BBox(x=left, y=top, w=max(1, right - left), h=max(1, bottom - top))


def _runs(good: list[bool], gap: int) -> list[tuple[int, int]]:
    """Index ranges `(first, last)` of consecutive True, joined across gaps of up to `gap`."""
    runs: list[list[int]] = []
    for index, flag in enumerate(good):
        if not flag:
            continue
        if runs and index - runs[-1][1] - 1 <= gap:
            runs[-1][1] = index
        else:
            runs.append([index, index])
    return [(a, b) for a, b in runs]


def refine(image: bytes, box: BBox) -> BBox:
    """`box` fitted to the text in `image`, in the image's own pixels. Falls back to the
    padded model box when the text cannot be measured."""
    try:
        picture = Image.open(io.BytesIO(image)).convert("RGB")
    except (OSError, ValueError):
        return box
    width, height = picture.size
    left = max(0, box.x - math.ceil(EXPAND_H * box.w))
    top = max(0, box.y - math.ceil(EXPAND_V * box.h))
    right = min(width, box.x + box.w + math.ceil(EXPAND_H * box.w))
    bottom = min(height, box.y + box.h + math.ceil(EXPAND_V * box.h))
    if right - left < 2 or bottom - top < 2:
        return fallback(box, width, height)
    crop = picture.crop((left, top, right, bottom))
    cw, ch = crop.size
    red, green, blue = crop.split()
    light = ImageChops.darker(ImageChops.darker(red, green), blue)
    data = light.point(lambda v: 255 if v >= LIGHT_MIN else 0).tobytes()

    rows = [data[r * cw : (r + 1) * cw].count(255) for r in range(ch)]
    runs = _runs([n >= ROW_MIN_PIXELS for n in rows], ROW_GAP)
    if not runs:
        return fallback(box, width, height)
    first, last = max(runs, key=lambda run: sum(rows[run[0] : run[1] + 1]))
    grew = True
    while grew:
        grew = False
        reach = math.ceil(TAIL_GAP_FRACTION * (last - first + 1))
        for a, b in runs:
            if a < first and first - b - 1 <= reach:
                first, grew = a, True
            elif b > last and a - last - 1 <= reach:
                last, grew = b, True
    text_h = last - first + 1
    band = data[first * cw : (last + 1) * cw]
    if band.count(255) < max(MIN_LIGHT_PIXELS, MIN_LIGHT_FRACTION * cw * ch):
        return fallback(box, width, height)

    col_min = max(2, math.ceil(COL_MIN_FRACTION * text_h))
    cols = [band[c::cw].count(255) for c in range(cw)]
    groups = _runs([n >= col_min for n in cols], math.ceil(COL_GAP_HEIGHTS * text_h))
    if not groups:
        return fallback(box, width, height)
    mass = [sum(cols[a : b + 1]) for a, b in groups]
    kept = [g for g, m in zip(groups, mass, strict=True) if m >= SPECK_SHARE * sum(mass)]
    x1, x2 = kept[0][0], kept[-1][1]

    margin = max(OUTLINE_MIN_PIXELS, math.ceil(OUTLINE_FRACTION * text_h))
    return _clamped(
        left + x1 - margin,
        top + first - margin,
        left + x2 + 1 + margin,
        top + last + 1 + margin,
        width,
        height,
    )
