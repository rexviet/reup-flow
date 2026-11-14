"""Where a collection puts the subtitle cover box.

A collection may shift the box up or down and set its width and height, each as a whole
percentage of the frame (`collections.cover_offset_pct`, `cover_width_pct`,
`cover_height_pct`). Unset, the box is the one found from the source subtitles, exactly
as before. The dashboard preview draws the same box with the same algorithm (TypeScript,
in the web service); both test it over the same table of cases.

Every rounding introduced here is half-up (`Math.round` in the browser), so the two
implementations agree on every frame, never only on most.
"""

import math
from dataclasses import dataclass

from localization.contracts.geometry import BBox


@dataclass(frozen=True)
class CoverGeometry:
    offset_pct: int | None = None
    width_pct: int | None = None
    height_pct: int | None = None

    @classmethod
    def of(cls, settings: object | None) -> "CoverGeometry":
        """The three settings as a `CollectionSnapshot` or a review manifest holds them."""
        return cls(
            offset_pct=getattr(settings, "cover_offset_pct", None),
            width_pct=getattr(settings, "cover_width_pct", None),
            height_pct=getattr(settings, "cover_height_pct", None),
        )

    @property
    def automatic(self) -> bool:
        return self.offset_pct is None and self.width_pct is None and self.height_pct is None


@dataclass(frozen=True)
class CoverBox:
    top: int
    height: int
    width_pct: float


def _half_up(value: float) -> int:
    return math.floor(value + 0.5)


def place_cover(
    frame_h: int, base_top: int, base_height: int, base_width_pct: float, geometry: CoverGeometry
) -> CoverBox:
    """The box a collection asks for, from the automatic one (`base_*`).

    The height is the collection's share of the frame, else the base height; the centre is
    the base box's, moved by the offset; the box is kept inside the frame; the width is the
    collection's share, else the base width. With nothing set the base box is returned as
    it is."""
    if geometry.automatic:
        return CoverBox(top=base_top, height=base_height, width_pct=base_width_pct)
    height = base_height
    if geometry.height_pct is not None:
        height = max(1, min(frame_h, _half_up(frame_h * geometry.height_pct / 100)))
    centre = base_top + base_height / 2 + _half_up(frame_h * (geometry.offset_pct or 0) / 100)
    top = max(0, min(_half_up(centre - height / 2), frame_h - height))
    width_pct = base_width_pct if geometry.width_pct is None else float(geometry.width_pct)
    return CoverBox(top=top, height=height, width_pct=width_pct)


def place_cover_region(frame_w: int, frame_h: int, base: BBox, geometry: CoverGeometry) -> BBox:
    """`place_cover` for a pixel box: a set width is centred in the frame, an unset one
    keeps the base box's own left edge and width."""
    if geometry.automatic:
        return base
    box = place_cover(frame_h, base.y, base.h, base.w / frame_w * 100, geometry)
    x, w = base.x, base.w
    if geometry.width_pct is not None:
        w = max(1, min(frame_w, _half_up(frame_w * geometry.width_pct / 100)))
        x = (frame_w - w) // 2
    return BBox(x=x, y=box.top, w=w, h=box.height)
