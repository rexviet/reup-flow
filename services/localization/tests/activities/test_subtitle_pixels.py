"""The subtitle box is fitted to the text's pixels, whatever the model's box was.

The frames are drawn with the repo's caption font: white text with a black outline on
a textured gradient, at the sizes the pipeline sees (a 576x1024 frame for the vision
model, and the 1080x1920 source). The model's box is then made deliberately sloppy.
"""

import asyncio
import io
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from PIL import Image, ImageChops, ImageDraw, ImageFont
from temporalio.testing import ActivityEnvironment

from localization.activities.detect_subtitles import DetectSubtitlesActivity, locate
from localization.activities.layout_captions import grow_cover
from localization.activities.subtitle_pixels import FALLBACK_PAD, LIGHT_MIN, fallback, refine
from localization.contracts import BBox
from localization.storage.object_store import ObjectStore
from tests.support.inputs import make_input

FONT = Path(__file__).resolve().parents[4] / "assets" / "fonts" / "BeVietnamPro-SemiBold.ttf"


@dataclass(frozen=True)
class Drawn:
    jpeg: bytes
    size: tuple[int, int]
    text: BBox  # the light pixels, measured on the decoded JPEG
    outline: BBox  # the glyphs with their dark outline


def _background(size: tuple[int, int]) -> Image.Image:
    """A gradient with noise: dark enough that no background pixel passes as text."""
    w, h = size
    gradient = Image.linear_gradient("L").resize((w, h)).point(lambda v: int(v * 0.45))
    noise = Image.frombytes("L", (w, h), random.Random(7).randbytes(w * h)).point(lambda v: v // 7)
    base = ImageChops.add(gradient, noise)
    return Image.merge(
        "RGB", (base, base.point(lambda v: v // 2 + 20), base.point(lambda v: v // 3))
    )


def _bbox_of_light(picture: Image.Image) -> BBox:
    r, g, b = picture.convert("RGB").split()
    mask = ImageChops.darker(ImageChops.darker(r, g), b).point(
        lambda v: 255 if v >= LIGHT_MIN else 0
    )
    left, top, right, bottom = mask.getbbox()
    return BBox(x=left, y=top, w=right - left, h=bottom - top)


def draw(
    text: str, size: tuple[int, int], font_px: int, stroke: int, top_fraction: float = 0.69,
    left: int | None = None, speck: bool = False,
) -> Drawn:  # fmt: skip
    w, h = size
    font = ImageFont.truetype(str(FONT), font_px)
    picture = _background(size)
    canvas = ImageDraw.Draw(picture)
    span = canvas.textlength(text, font=font)
    xy = ((w - span) / 2 if left is None else left, h * top_fraction)
    canvas.text(
        xy, text, font=font, fill=(255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0)
    )
    if speck:  # a bright bar above the text: a sign's edge, a window
        y = int(xy[1]) - font_px // 2
        canvas.rectangle((int(xy[0]), y, int(xy[0]) + 40, y + 2), fill=(255, 255, 255))
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=88)
    decoded = Image.open(io.BytesIO(out.getvalue())).convert("RGB")
    solid = Image.new("L", size, 0)
    ImageDraw.Draw(solid).text(xy, text, font=font, fill=255, stroke_width=stroke, stroke_fill=255)
    left_, top_, right_, bottom_ = solid.getbbox()
    outline = BBox(x=left_, y=top_, w=right_ - left_, h=bottom_ - top_)
    # Without the speck, the light pixels are exactly the text.
    clean = _bbox_of_light(decoded) if not speck else None
    if clean is None:
        plain = draw(text, size, font_px, stroke, top_fraction, left)
        clean = plain.text
    return Drawn(out.getvalue(), size, clean, outline)


def contains(outer: BBox, inner: BBox) -> bool:
    return (
        outer.x <= inner.x
        and outer.y <= inner.y
        and outer.x + outer.w >= inner.x + inner.w
        and outer.y + outer.h >= inner.y + inner.h
    )


SIZES = [((576, 1024), 38, 3), ((1080, 1920), 72, 5)]
TEXT = "Chào các bạn hôm nay"


def sloppy(t: BBox, frame: tuple[int, int]) -> dict[str, BBox]:
    fw, fh = frame
    wide_x = max(0, t.x - round(0.2 * t.w))
    return {
        "shifted down": BBox(x=t.x, y=t.y + round(0.2 * t.h), w=t.w, h=t.h),
        "too tall": BBox(x=t.x, y=t.y - round(0.3 * t.h), w=t.w, h=round(1.6 * t.h)),
        "too wide": BBox(x=wide_x, y=t.y, w=min(fw - wide_x, round(1.4 * t.w)), h=t.h),
        "clips the bottom": BBox(x=t.x, y=t.y, w=t.w, h=round(0.8 * t.h)),
        "exact": t,
    }


@pytest.mark.parametrize(("size", "font_px", "stroke"), SIZES)
def test_every_sloppy_box_is_fitted_to_the_same_tight_text_box(
    size: tuple[int, int], font_px: int, stroke: int
) -> None:
    drawn = draw(TEXT, size, font_px, stroke)
    t = drawn.text
    results = {}
    for name, model_box in sloppy(t, size).items():
        fitted = refine(drawn.jpeg, model_box)
        results[name] = fitted
        assert contains(fitted, t), f"{name}: a light text pixel is outside {fitted}"
        assert contains(fitted, drawn.outline), f"{name}: the outline is cut off"
        assert fitted.h <= 1.5 * t.h, f"{name}: {fitted.h} px for text {t.h} px tall"
        assert fitted.w <= 1.15 * t.w, f"{name}: {fitted.w} px for text {t.w} px wide"
    # Whatever the model said, the result is the same box: it comes from the pixels.
    assert len({tuple(b.model_dump().values()) for b in results.values()}) == 1


@pytest.mark.parametrize(("size", "font_px", "stroke"), SIZES)
def test_a_bright_bar_above_the_text_does_not_stretch_the_box(
    size: tuple[int, int], font_px: int, stroke: int
) -> None:
    drawn = draw(TEXT, size, font_px, stroke, speck=True)
    t = drawn.text
    model_box = BBox(x=t.x, y=t.y - round(0.3 * t.h), w=t.w, h=round(1.6 * t.h))
    fitted = refine(drawn.jpeg, model_box)
    assert contains(fitted, t)
    assert fitted.h <= 1.5 * t.h


@pytest.mark.parametrize(("size", "font_px", "stroke"), SIZES)
def test_without_light_pixels_the_model_box_is_kept_with_a_ten_percent_pad(
    size: tuple[int, int], font_px: int, stroke: int
) -> None:
    out = io.BytesIO()
    _background(size).save(out, "JPEG", quality=88)  # dark: no subtitle pixels at all
    box = BBox(x=100, y=int(size[1] * 0.69), w=300, h=60)
    fitted = refine(out.getvalue(), box)
    assert FALLBACK_PAD == 0.10
    assert fitted == BBox(x=94, y=box.y - 6, w=312, h=72)
    assert fitted == fallback(box, *size)


def test_the_fallback_pad_stays_inside_the_frame() -> None:
    fitted = fallback(BBox(x=0, y=1000, w=576, h=24), 576, 1024)
    assert fitted.x == 0 and fitted.x + fitted.w == 576 and fitted.y + fitted.h == 1024


def test_the_union_over_lines_of_different_widths_spans_the_widest() -> None:
    size = (1080, 1920)
    lines = ["Chào", "Chào các bạn", "Chào các bạn hôm nay mình"]
    truths, fitted = [], []
    for line in lines:
        drawn = draw(line, size, 72, 5)
        truths.append(drawn.text)
        fitted.append(refine(drawn.jpeg, sloppy(drawn.text, size)["too tall"]))
    region, _ = locate([*fitted, *fitted], *size, 0)
    assert region is not None
    widest = max(truths, key=lambda b: b.w)
    assert contains(region, widest)
    assert region.w <= 1.15 * widest.w  # not the model's spread


# --- the cover the activity and the layout end up with ---------------------------------------


@pytest.fixture
def store():  # noqa: ANN201
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        yield ObjectStore(client, "reup-media")


class SloppyOcr:
    def __init__(self, box: BBox, size: tuple[int, int]) -> None:
        self.box, self.size = box, size

    async def detect_region(self, frame: bytes, width: int, height: int) -> BBox | None:
        assert (width, height) == self.size
        await asyncio.sleep(0)
        return self.box


async def test_the_cover_for_the_real_clip_fits_the_text_not_the_models_box(
    store: ObjectStore, tmp_path: Path
) -> None:
    """Text about 660 x 85 px at x 200-860, y 1296-1380 of a 1080x1920 frame. The
    model's box on the real clip was far too tall and ran to the frame's right edge."""
    source = (1080, 1920)
    drawn = draw("Chào các bạn nhé", source, 76, 5, top_fraction=0.6667, left=200)
    t = drawn.text
    assert 640 <= t.w <= 700 and 70 <= t.h <= 90, t  # the clip's text, within a few percent
    still = tmp_path / "frame.png"
    Image.open(io.BytesIO(drawn.jpeg)).save(still)
    video = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-loop", "1", "-framerate", "4", "-i",
         str(still), "-t", "3", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )  # fmt: skip
    spec = make_input()
    store.put_file(spec.video_key, video)
    scaled = (576, 1024)
    factor = scaled[0] / source[0]
    # What the model said on the real clip, in the frame it saw: starts 55 px above the
    # text, ends above its bottom, runs to the right edge.
    model = BBox(x=round(206 * factor), y=round(1241 * factor), w=scaled[0] - 110, h=61)
    result = await ActivityEnvironment().run(
        DetectSubtitlesActivity(store, SloppyOcr(model, scaled)).detect_subtitles, spec
    )
    assert result.region is not None
    cover = grow_cover(result.region, *source)
    assert contains(cover, t) and contains(cover, drawn.outline)
    assert cover.h <= 130, cover
    assert cover.w <= 1.15 * t.w + 80
    assert cover.x + cover.w < source[0] - 100  # nowhere near the right edge
