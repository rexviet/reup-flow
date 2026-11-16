"""The watermark search, on frames drawn here: no fixture video, the spike's cases."""

import shutil
import subprocess
import threading

import cv2
import numpy as np
import pytest

from localization.contracts import BBox
from localization.watermarks.template_match import (
    FRAME_MS,
    Template,
    find,
    find_in_frames,
)

W, H = 360, 640
TEXT = "@kenh.phim"


def _scene(i: int, width: int = W, height: int = H) -> np.ndarray:
    """A grey frame with moving shapes, as a video's picture behind its watermark."""
    rng = np.random.default_rng(i)
    frame = np.full((height, width), 90, np.uint8)
    for _ in range(6):
        x, y = int(rng.integers(0, width - 40)), int(rng.integers(140, height - 200))
        cv2.rectangle(frame, (x, y), (x + 40, y + 30), int(rng.integers(30, 220)), -1)
    return frame


def _draw(frame: np.ndarray, x: int, y: int, scale: float = 1.0, text: str = TEXT) -> None:
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9 * scale, 245, 2)


def _template(scale: float = 1.0, frame_height: int = H) -> Template:
    """The watermark cut from a frame `frame_height` high, as a person cuts it."""
    canvas = np.full((60, 260), 90, np.uint8)
    _draw(canvas, 10, 40, scale)
    ys, xs = np.nonzero(canvas > 200)
    box = canvas[ys.min() - 4 : ys.max() + 5, xs.min() - 4 : xs.max() + 5].copy()
    return Template(image=box, frame_height=frame_height)


def _search(video: list[np.ndarray], subtitle: BBox | None = None, template=None):
    return find_in_frames(
        lambda: iter(video),
        [template or _template()],
        subtitle,
        width=video[0].shape[1],
        height=video[0].shape[0],
        duration_ms=len(video) * FRAME_MS,
    )


def _video(n: int, place, **kw) -> list[np.ndarray]:
    """`n` frames; `place(i)` is a list of (x, y) where the watermark is drawn on frame i."""
    frames = []
    for i in range(n):
        frame = _scene(i, **kw)
        for x, y in place(i):
            _draw(frame, x, y)
        frames.append(frame)
    return frames


def test_the_watermark_is_found_where_and_while_it_shows() -> None:
    video = _video(20, lambda i: [(16, 50)] if 4 <= i < 16 else [])
    (mark,) = _search(video)
    assert (mark.start_ms, mark.end_ms) == (4 * FRAME_MS, 16 * FRAME_MS)
    assert mark.region.x <= 16 and mark.region.y <= 50 - 20
    assert mark.region.x + mark.region.w >= 16 + 150 and mark.region.y + mark.region.h >= 50


def test_a_watermark_missed_for_a_moment_stays_blurred_through_the_gap() -> None:
    """On a background close to its colour the watermark is missed for a few frames."""
    video = _video(30, lambda i: [] if 10 <= i < 15 else [(16, 50)])
    (mark,) = _search(video)
    assert (mark.start_ms, mark.end_ms) == (0, 30 * FRAME_MS)


def test_a_watermark_gone_for_longer_is_two() -> None:
    video = _video(30, lambda i: [] if 8 <= i < 16 else [(16, 50)])
    first, second = _search(video)
    assert (first.end_ms, second.start_ms) == (8 * FRAME_MS, 16 * FRAME_MS)


def test_two_images_of_one_watermark_are_one() -> None:
    """The same watermark cut twice, at two sizes: both match, one blur."""
    video = _video(12, lambda i: [(16, 50)])
    marks = find_in_frames(
        lambda: iter(video), [_template(), _template(1.1, frame_height=int(H * 1.1))], None,
        width=W, height=H, duration_ms=len(video) * FRAME_MS,
    )  # fmt: skip
    assert len(marks) == 1


def test_a_watermark_that_moves_is_one_per_place() -> None:
    video = _video(20, lambda i: [(16, 50)] if i < 10 else [(W - 190, 50)])
    left, right = sorted(_search(video), key=lambda m: m.region.x)
    assert (left.start_ms, left.end_ms) == (0, 10 * FRAME_MS)
    assert (right.start_ms, right.end_ms) == (10 * FRAME_MS, 20 * FRAME_MS)
    assert left.region.x <= 16 and right.region.x >= W - 190 - 8


def test_the_same_watermark_in_two_corners_at_once_is_two() -> None:
    video = _video(12, lambda i: [(16, 50), (W - 190, 50)])
    marks = _search(video)
    assert len(marks) == 2
    assert {m.start_ms for m in marks} == {0}


def test_a_watermark_shown_larger_than_it_was_cut_is_found() -> None:
    """The image was cut from a 640-high frame; the video is 800 high, so it shows 1.25×."""
    height, width = 800, 450
    frames = []
    for i in range(12):
        frame = _scene(i, width, height)
        _draw(frame, 20, 64, scale=1.25)
        frames.append(frame)
    (mark,) = _search(frames)
    assert mark.region.h >= 25


def test_text_that_never_stays_in_one_place_is_not_a_watermark() -> None:
    video = _video(16, lambda i: [(10 + 40 * (i % 5), 60 + 90 * (i % 4))])
    assert _search(video) == ()


def test_a_match_inside_the_subtitle_band_is_subtitle_text() -> None:
    video = _video(12, lambda i: [(100, 560)])
    band = BBox(x=0, y=520, w=W, h=60)
    assert _search(video, band) == ()
    assert len(_search(video)) == 1


def test_a_dark_frame_matches_nothing() -> None:
    video = [np.full((H, W), 8, np.uint8) for _ in range(12)]
    assert _search(video) == ()


def test_another_text_is_not_the_watermark() -> None:
    video = []
    for i in range(12):
        frame = _scene(i)
        _draw(frame, 16, 50, text="SALE 50% OFF")
        video.append(frame)
    assert _search(video) == ()


def test_no_image_finds_nothing_without_reading_a_frame() -> None:
    def never():
        raise AssertionError("read")

    assert find_in_frames(never, [], None, width=W, height=H, duration_ms=1000) == ()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_search_reads_a_real_video(tmp_path) -> None:
    video = tmp_path / "source.mp4"
    encode = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{W}x{H}", "-r", "10", "-i", "-", "-pix_fmt", "yuv420p", "-c:v", "libx264",
         "-crf", "18", str(video)],
        stdin=subprocess.PIPE,
    )  # fmt: skip
    assert encode.stdin is not None
    for i in range(60):  # 6 s at 10 fps; the watermark from 2 s
        frame = _scene(i // 5)
        if i >= 20:
            _draw(frame, 16, 50)
        encode.stdin.write(frame.tobytes())
    encode.stdin.close()
    assert encode.wait() == 0
    (mark,) = find(video, [_template()], None, width=W, height=H, duration_ms=6000)
    assert mark.start_ms in (1500, 2000) and mark.end_ms == 6000
    cancelled = threading.Event()
    cancelled.set()
    assert find(video, [_template()], None, width=W, height=H, duration_ms=6000,
                cancel=cancelled) == ()  # fmt: skip
