"""The font the layout measures with is the font the renderer draws with.

A line is broken and sized by measuring it with PIL; if the renderer drew it with another
font (or the same font at another size) the line would overflow its box or leave a gap. So
this test does not compare two configs: it lays captions out through the real layout code,
renders them through the real renderer (Chromium), cuts frames out of the video, finds
the pixels the text is made of, and compares their width with what PIL measured.

Needs `pnpm install` for services/renderer and ffmpeg. Set LOC_SKIP_RENDERER=1 to skip it
on a machine that has neither; a missing prerequisite is otherwise a failure, never a skip.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from localization.activities.layout_captions import FontMeter, lay_out
from localization.config import DEFAULT_CAPTION_FONT
from localization.contracts import (
    BBox,
    DubbedAudio,
    DubSegment,
    DubSegmentsDocument,
    IngestResult,
    LocalizedScript,
    LocalizedSegment,
    SubtitleTrack,
)
from localization.contracts.export import REPO_ROOT

RENDERER = REPO_ROOT / "services" / "renderer"
FONT = str(DEFAULT_CAPTION_FONT)
TOLERANCE_PX = 4
WHITE = 160  # a pixel this bright, on a dark cover, belongs to the text
SECONDS = 3

pytestmark = pytest.mark.skipif(
    os.environ.get("LOC_SKIP_RENDERER") == "1", reason="LOC_SKIP_RENDERER=1"
)

TEXTS = [
    "Xin chào các bạn, hôm nay",
    "Hôm nay chúng ta sẽ cùng nhau học cách nấu một nồi phở bò thật ngon tại nhà",
]


def _need(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.fail(f"{tool} is not installed; this test measures real rendered pixels")


def _script() -> LocalizedScript:
    segments = tuple(
        LocalizedSegment(
            id=i,
            start_ms=i * 1500,
            end_ms=(i + 1) * 1500,
            slot_ms=1500,
            src_zh="你好",
            text_vi=text,
            on_screen=False,
        )
        for i, text in enumerate(TEXTS)
    )
    return LocalizedScript(aweme_id="7001", version=1, prompt_version="t", segments=segments)


@pytest.fixture(scope="module")
def render(tmp_path_factory: pytest.TempPathFactory):
    _need("ffmpeg")
    _need("pnpm")
    if not (RENDERER / "node_modules").exists():
        pytest.fail("services/renderer has no node_modules; run `pnpm install` at the repo root")
    work = tmp_path_factory.mktemp("font-parity")
    meter = FontMeter(FONT)
    script = _script()
    doc = DubSegmentsDocument(
        aweme_id="7001",
        script_version=1,
        segments=tuple(
            DubSegment(
                id=s.id,
                text_vi=s.text_vi,
                tts_key=f"cache/tts/{'a' * 64}.wav",
                duration_ms=1000,
                stretch_ratio=1.0,
                overflow_unresolved=False,
            )
            for s in script.segments
        ),
    )
    ingest = IngestResult(
        vocals_key="work/7001/r0/vocals.wav",
        bgm_key="work/7001/r0/bgm.wav",
        duration_ms=SECONDS * 1000,
        width=1080,
        height=1920,
        fps=30,
    )
    audio = work / "audio.wav"
    video = work / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
         f"sine=frequency=300:duration={SECONDS}", "-ar", "24000", "-ac", "1", str(audio)],
        check=True,
    )  # fmt: skip
    # A dark, plain picture: the text is then the only bright thing in the caption box.
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
         f"color=c=black:s=1080x1920:r=30:d={SECONDS}", "-c:v", "libx265", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-tag:v", "hvc1", str(video)],
        check=True,
    )  # fmt: skip
    spec = lay_out(
        script=script,
        doc=doc,
        subtitles=SubtitleTrack(
            subtitle_key="work/7001/r0/subs.json", region=BBox(x=60, y=1500, w=960, h=160)
        ),
        ingest=ingest,
        dub=DubbedAudio(
            audio_key="work/7001/r1/dubbed.wav",
            audio_sha256="b" * 64,
            script_key="work/7001/r0/script.v1.json",
            script_version=1,
            segments_key="work/7001/r1/dub-segments.json",
        ),
        source_key="work/7001/r0/source.mp4",
        meter=meter,
        cover_color="#101010",
        cover_opacity=0.92,
    )
    spec_file = work / "spec.json"
    spec_file.write_text(spec.model_dump_json())
    out = work / "out.mp4"
    result = subprocess.run(
        ["pnpm", "-C", str(RENDERER), "run", "render:local", "--",
         "--spec", str(spec_file), "--source", str(video), "--audio", str(audio),
         "--font", FONT, "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=300,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return spec, out, meter


def _caption_pixels(video: Path, seconds: float, box: BBox) -> Image.Image:
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{seconds}", "-i", str(video),
         "-frames:v", "1", "-vf", f"crop={box.w}:{box.h}:{box.x}:{box.y}",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    return Image.frombytes("L", (box.w, box.h), raw)


def _line_bands(image: Image.Image) -> list[tuple[int, int, int]]:
    """(top row, left column, right column) of each run of rows that holds text pixels."""
    mask = image.point(lambda v: 255 if v > WHITE else 0)
    width, height = mask.size
    data = mask.tobytes()
    bands: list[list[int]] = []
    inside = False
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        columns = [x for x, v in enumerate(row) if v]
        if columns and not inside:
            bands.append([y, columns[0], columns[-1]])
            inside = True
        elif columns:
            bands[-1][1] = min(bands[-1][1], columns[0])
            bands[-1][2] = max(bands[-1][2], columns[-1])
        else:
            inside = False
    return [(top, left, right) for top, left, right in bands]


def _merge_lines(bands: list[tuple[int, int, int]], lines: int) -> list[tuple[int, int, int]]:
    """Accents and descenders can split one text line into two row runs a few pixels
    apart; join runs until `lines` remain."""
    bands = list(bands)
    while len(bands) > lines:
        gaps = [bands[i + 1][0] - bands[i][0] for i in range(len(bands) - 1)]
        i = gaps.index(min(gaps))
        top, left, right = bands[i]
        _, left2, right2 = bands.pop(i + 1)
        bands[i] = (top, min(left, left2), max(right, right2))
    return bands


def test_the_text_is_drawn_as_wide_as_the_layout_measured_it(render) -> None:
    spec, video, meter = render
    assert len(spec.captions) == len(TEXTS)
    for caption in spec.captions:
        middle = (caption.start_ms + caption.end_ms) / 2000
        image = _caption_pixels(video, middle, spec.caption_box)
        lines = caption.text.split("\n")
        bands = _merge_lines(_line_bands(image), len(lines))
        assert len(bands) == len(lines), f"{len(bands)} lines drawn, {len(lines)} laid out"
        for line, (_, left, right) in zip(lines, bands, strict=True):
            drawn = right - left + 1
            measured = meter.ink_width(line, caption.font_px)
            assert abs(drawn - measured) <= TOLERANCE_PX, (
                f"{line!r} at {caption.font_px}px: drawn {drawn}px, measured {measured}px"
            )


def test_the_text_stays_inside_the_box_it_was_fitted_to(render) -> None:
    spec, video, _ = render
    for caption in spec.captions:
        middle = (caption.start_ms + caption.end_ms) / 2000
        image = _caption_pixels(video, middle, spec.caption_box)
        bands = _line_bands(image)
        assert bands, "no text drawn"
        assert all(left > 0 and right < spec.caption_box.w - 1 for _, left, right in bands)
