"""Watermarks blurred where and while they show, in the render and the review project."""

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageFilter, ImageStat

from localization.activities.render import _filter_graph, render_file
from localization.activities.review_project import resolve_project
from localization.contracts import BBox, EditSpec, Watermark
from localization.contracts.spec_hash import canonical_spec_json
from localization.review_bridge.repository import Repository
from tests.support.review_project import project_inputs

W, H = 1080, 1920
SUBTITLE = BBox(x=140, y=1500, w=800, h=60)
LEFT = BBox(x=40, y=60, w=300, h=40)
RIGHT = BBox(x=740, y=60, w=300, h=40)


# --- the render ----------------------------------------------------------------------------


def _spec(blurs: tuple[Watermark, ...] = ()) -> EditSpec:
    return EditSpec.model_validate(
        {
            "source_key": "work/7001/g1/r0/source.mp4",
            "audio_key": "work/7001/g1/r1/dubbed.wav",
            "audio_sha256": "0" * 64,
            "script_version": 1,
            "width": 320,
            "height": 240,
            "fps": 10,
            "duration_ms": 2000,
            "cover": None,
            "caption_box": {"x": 10, "y": 180, "w": 300, "h": 40},
            "captions": [],
            "font": "font.ttf",
            "font_sha256": "1" * 64,
            "blurs": [b.model_dump() for b in blurs],
        }
    )


def test_a_spec_without_blurs_keeps_the_hash_it_had() -> None:
    assert '"blurs"' not in canonical_spec_json(_spec())
    mark = Watermark(region=BBox(x=4, y=4, w=60, h=12), start_ms=0, end_ms=1000)
    assert '"blurs"' in canonical_spec_json(_spec((mark,)))


def test_the_render_graph_blurs_each_watermark_only_while_it_shows() -> None:
    left = Watermark(region=BBox(x=5, y=3, w=61, h=13), start_ms=0, end_ms=1000)
    right = Watermark(region=BBox(x=250, y=4, w=60, h=12), start_ms=500, end_ms=2000)
    graph = _filter_graph(_spec((left, right)), captions_at=None)
    assert graph.count("boxblur=") == 2
    assert "enable='between(t,0.000,1.000)'" in graph
    assert "enable='between(t,0.500,2.000)'" in graph
    # Even edges for 4:2:0: the odd box is grown, not shifted out of the text.
    assert "crop=62:14:4:2" in graph
    assert _filter_graph(_spec(), captions_at=None).count("boxblur") == 0


def _sharpness(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """Mean edge strength: what a blur takes away, whatever the colours are."""
    edges = image.crop(box).convert("L").filter(ImageFilter.FIND_EDGES)
    return ImageStat.Stat(edges).mean[0]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_rendered_watermark_is_blurred_during_its_time_and_sharp_after(tmp_path) -> None:
    from localization.config import DEFAULT_CAPTION_FONT

    source, audio = tmp_path / "s.mp4", tmp_path / "a.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
         "testsrc=size=320x240:rate=10:duration=2", "-pix_fmt", "yuv420p", str(source)],
        check=True,
    )  # fmt: skip
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=300:duration=2", "-ar", "44100", "-ac", "2", str(audio)],
        check=True,
    )  # fmt: skip
    # testsrc draws a frame counter (sharp text) in the upper left.
    region = BBox(x=0, y=0, w=160, h=80)
    spec = _spec((Watermark(region=region, start_ms=0, end_ms=1000),)).model_copy(
        update={"font": Path(DEFAULT_CAPTION_FONT).name}
    )
    out = tmp_path / "out.mp4"
    render_file(
        spec, source=source, audio=audio, font_path=str(DEFAULT_CAPTION_FONT), out=out,
        work_dir=tmp_path,
    )  # fmt: skip

    def frame(index: int) -> Image.Image:
        raw = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-vf",
             f"select=eq(n\\,{index})", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt",
             "rgb24", "-"],
            capture_output=True, check=True,
        ).stdout  # fmt: skip
        return Image.frombytes("RGB", (320, 240), raw)

    box = (0, 0, 160, 80)
    blurred, sharp = _sharpness(frame(5), box), _sharpness(frame(15), box)
    assert blurred < sharp * 0.5, (blurred, sharp)


# --- the review project --------------------------------------------------------------------


def test_the_review_project_blurs_the_watermarks_in_every_revision(review_store, tmp_path):
    left = Watermark(region=BBox(x=4, y=6, w=100, h=20), start_ms=0, end_ms=1200)
    right = Watermark(region=BBox(x=500, y=6, w=100, h=20), start_ms=600, end_ms=1200)
    builder, spec, dub, ingest, subs = project_inputs(
        review_store, tmp_path, watermarks=(left, right)
    )
    ref = builder._build(spec, dub, ingest, subs, "engine")
    _, manifest, _ = resolve_project(review_store, ref.project_key)
    assert manifest.watermarks == (left, right)
    html = review_store.get_bytes(manifest.html.key, produced_by="test").decode()
    assert html.count('class="clip watermark-mask"') == 2
    assert 'id="wm-0"' in html and 'data-track-index="4"' in html
    assert 'id="wm-1"' in html and 'data-track-index="5"' in html
    assert "left:500px;top:6px;width:100px;height:20px" in html
    assert 'data-start="0.6" data-duration="0.6"' in html
    assert "backdrop-filter:blur(" in html

    repo = Repository(review_store, tmp_path / "review")
    _, base, _ = repo.current("7001", 1)
    revision = repo.prepare(base, base.cues, {})
    again = repo.read_asset(revision.html).decode()
    assert again.count('class="clip watermark-mask"') == 2


def test_a_project_without_watermarks_has_no_blur(review_store, tmp_path):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    ref = builder._build(spec, dub, ingest, subs, "engine")
    _, manifest, _ = resolve_project(review_store, ref.project_key)
    html = review_store.get_bytes(manifest.html.key, produced_by="test").decode()
    assert "watermark-mask" not in html
