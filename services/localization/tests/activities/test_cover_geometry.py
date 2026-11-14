"""Where a collection puts the cover box. The table is shared with the web preview's test
(the same cases, the same answers), so the dashboard draws the box the project draws."""

import pytest

from localization.activities.cover_geometry import (
    CoverGeometry,
    place_cover,
    place_cover_region,
)
from localization.contracts import BBox, CollectionSnapshot


def review_box(width, height, region, offset, width_pct, height_pct):
    """The review project's base box, then the collection's geometry."""
    top, box_h = region if region is not None else (round(height * 0.8), round(height * 0.14))
    geometry = CoverGeometry(offset_pct=offset, width_pct=width_pct, height_pct=height_pct)
    box = place_cover(height, top, box_h, 60, geometry)
    return box.top, box.height, box.width_pct


# W×H | region (y,h) | offset | width | height | → top, height, width %
CASES = [
    ((720, 1280), (1000, 120), None, None, None, (1000, 120, 60)),
    ((720, 1280), (1000, 120), 3, 100, None, (1038, 120, 100)),
    ((720, 1280), (1000, 120), None, None, 12, (983, 154, 60)),
    ((720, 1280), (1000, 120), 50, None, None, (1160, 120, 60)),  # clamped
    ((720, 1280), None, None, 80, None, (1024, 179, 80)),  # fallback 0.8H, 0.14H
]


@pytest.mark.parametrize(("frame", "region", "offset", "width", "height", "expected"), CASES)
def test_the_shared_table(frame, region, offset, width, height, expected) -> None:
    assert review_box(*frame, region, offset, width, height) == expected


def test_a_box_moved_up_past_the_frame_stops_at_its_top_edge() -> None:
    box = place_cover(1280, 40, 120, 60, CoverGeometry(offset_pct=-50))
    assert (box.top, box.height) == (0, 120)


def test_the_largest_box_still_fits_the_frame() -> None:
    box = place_cover(1280, 1200, 80, 60, CoverGeometry(offset_pct=50, height_pct=50))
    assert (box.top, box.height) == (640, 640)


def test_half_a_pixel_rounds_up_as_in_the_browser() -> None:
    # 2% of 1225 is 24.5 px: 25, as Math.round gives, where Python's round gives 24.
    assert place_cover(1225, 1000, 100, 60, CoverGeometry(offset_pct=2)).top == 1025
    # A 25 px box (2% of 1250) centred on 1049: its top at 1036.5, so 1037.
    assert place_cover(1250, 999, 100, 60, CoverGeometry(height_pct=2)).top == 1037


def test_the_settings_are_read_from_a_snapshot_or_a_manifest() -> None:
    snapshot = CollectionSnapshot(
        id="0b6f3c1e-2d4a-4f8e-9a51-7c2e8d9f0a13",
        name="x",
        cover_offset_pct=-5,
        cover_width_pct=100,
        cover_height_pct=10,
    )
    assert CoverGeometry.of(snapshot) == CoverGeometry(-5, 100, 10)
    assert CoverGeometry.of(None).automatic


def test_a_pixel_box_without_settings_is_returned_as_it_is() -> None:
    base = BBox(x=13, y=1500, w=1001, h=100)
    assert place_cover_region(1080, 1920, base, CoverGeometry()) is base


def test_a_pixel_box_takes_the_set_width_centred_and_keeps_its_own_otherwise() -> None:
    base = BBox(x=40, y=1500, w=1000, h=100)
    assert place_cover_region(1080, 1920, base, CoverGeometry(width_pct=50)) == BBox(
        x=270, y=1500, w=540, h=100
    )
    # 3% of 1920 is 57.6 px: 58 px lower, same left edge and width.
    assert place_cover_region(1080, 1920, base, CoverGeometry(offset_pct=3)) == BBox(
        x=40, y=1558, w=1000, h=100
    )
