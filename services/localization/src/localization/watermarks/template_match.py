"""Where and while a collection's watermark images show on a source video.

Each image is searched on every frame at 2 fps by template matching on edges (Canny,
softened): an edge picture ignores the background behind semi-transparent text, which
plain grey matching does not. The method and its constants are the spike's
(`spikes/watermark_template_spike.py`), measured on 17 real videos of two collections:

- The sizes an image shows at are calibrated per video: every factor in `SCALES` (of the
  video's height over the height of the frame the image was cut from) is tried on
  `CALIBRATION_FRAMES` frames spread over the video, and the best `MAX_SCALES` that reach
  `THRESHOLD` are searched.
- A match must hold `MIN_INK` of the image's edge pixels (on a flat patch the correlation
  is taken over noise and can score high on nothing), must not lie mostly inside the
  subtitle band, and its place must be held for `MIN_RUN` frames, gaps of up to
  `MAX_GAP` frames bridged.

Each run of frames at one place becomes one `Watermark`: the union of its matches, from
the first frame of the run to the end of the last.
"""

import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

import cv2
import numpy as np

from localization.contracts import BBox, Watermark

SAMPLE_FPS = 2
FRAME_MS = 1000 // SAMPLE_FPS
THRESHOLD = 0.7
# Matches kept per image size per frame: a watermark in two corners at once is two.
MAX_HITS = 4
SCALES = tuple(round(0.6 + 0.05 * i, 2) for i in range(19))  # 0.6 .. 1.5
CALIBRATION_FRAMES = 24
MAX_SCALES = 3
MIN_INK = 0.5
INSIDE_SUBTITLE = 0.5
MIN_RUN = 4  # 2 s at 2 fps
MAX_GAP = 2
CANNY_LOW, CANNY_HIGH = 80, 200
SOFTEN = (7, 7)
# Longest the decoder may take for the whole video before the search gives up.
DECODE_TIMEOUT_S = 3600


@dataclass(frozen=True)
class Template:
    """One image of a collection, grey, and the height of the frame it was cut from."""

    image: np.ndarray
    frame_height: int


@dataclass(frozen=True)
class _Variant:
    template: int
    edges: np.ndarray
    ink: int


@dataclass(frozen=True)
class _Hit:
    template: int
    box: BBox
    score: float


@dataclass
class _Place:
    template: int
    x: int
    y: int
    w: int
    h: int
    frames: list[tuple[int, BBox]] = field(default_factory=list)


def edges(image: np.ndarray) -> np.ndarray:
    """Edges, softened: a watermark shown a little larger or smaller than its image still
    lands on the image's edges, which hard one-pixel edges do not."""
    return cv2.GaussianBlur(cv2.Canny(image, CANNY_LOW, CANNY_HIGH), SOFTEN, 0)


def _resized(image: np.ndarray, scale: float) -> np.ndarray:
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=interpolation)


def _fits(template: np.ndarray, picture: np.ndarray) -> bool:
    return 0 < template.shape[0] < picture.shape[0] and 0 < template.shape[1] < picture.shape[1]


def frames(
    video, width: int, height: int, cancel: threading.Event | None = None
) -> Iterator[np.ndarray]:
    """Every frame at `SAMPLE_FPS`, grey, at the source size. Setting `cancel` stops the
    decoder."""
    decode = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vf", f"fps={SAMPLE_FPS}",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE,
    )  # fmt: skip
    size = width * height
    try:
        assert decode.stdout is not None
        while (chunk := decode.stdout.read(size)) and len(chunk) == size:
            if cancel is not None and cancel.is_set():
                return
            yield np.frombuffer(chunk, np.uint8).reshape(height, width)
    finally:
        decode.kill()
        decode.wait(timeout=DECODE_TIMEOUT_S)


def _calibrate(
    pictures: Iterable[np.ndarray], templates: Sequence[Template], height: int, every: int
) -> list[list[float]]:
    """The scales each template is searched at in this video (see the module doc)."""
    variants = [
        {f: edges(_resized(t.image, height / t.frame_height * f)) for f in SCALES}
        for t in templates
    ]
    best: list[dict[float, float]] = [{} for _ in templates]
    for i, frame in enumerate(pictures):
        if i % every:
            continue
        picture = edges(frame)
        for n, by_factor in enumerate(variants):
            for factor, template in by_factor.items():
                if not _fits(template, picture):
                    continue
                score = float(cv2.matchTemplate(picture, template, cv2.TM_CCOEFF_NORMED).max())
                best[n][factor] = max(best[n].get(factor, 0.0), score)
    kept = []
    for t, scores in zip(templates, best, strict=True):
        good = sorted(((s, f) for f, s in scores.items() if s >= THRESHOLD), reverse=True)
        kept.append([height / t.frame_height * f for _, f in good[:MAX_SCALES]])
    return kept


def _peaks(result: np.ndarray, tw: int, th: int) -> list[tuple[int, int, float]]:
    found = []
    result = result.copy()
    for _ in range(MAX_HITS):
        _, best, _, (x, y) = cv2.minMaxLoc(result)
        if best < THRESHOLD:
            break
        found.append((x, y, float(best)))
        # One watermark is one match: its neighbourhood is not searched again.
        result[max(0, y - th) : y + th, max(0, x - tw) : x + tw] = -1
    return found


def _inside(box: BBox, region: BBox) -> float:
    ix = max(0, min(box.x + box.w, region.x + region.w) - max(box.x, region.x))
    iy = max(0, min(box.y + box.h, region.y + region.h) - max(box.y, region.y))
    return ix * iy / (box.w * box.h)


def _search(frame: np.ndarray, variants: Sequence[_Variant], subtitle: BBox | None) -> list[_Hit]:
    hard = cv2.Canny(frame, CANNY_LOW, CANNY_HIGH)
    picture = cv2.GaussianBlur(hard, SOFTEN, 0)
    hits = []
    for v in variants:
        if not _fits(v.edges, picture):
            continue
        th, tw = v.edges.shape
        result = cv2.matchTemplate(picture, v.edges, cv2.TM_CCOEFF_NORMED)
        for x, y, score in _peaks(result, tw, th):
            if np.count_nonzero(hard[y : y + th, x : x + tw]) < MIN_INK * v.ink:
                continue
            box = BBox(x=x, y=y, w=tw, h=th)
            if subtitle is not None and _inside(box, subtitle) >= INSIDE_SUBTITLE:
                continue
            hits.append(_Hit(v.template, box, score))
    # The same watermark matched at two sizes is one: the better is kept.
    kept: list[_Hit] = []
    for hit in sorted(hits, key=lambda h: -h.score):
        b = hit.box
        if all(
            abs(b.x - k.box.x) > k.box.w // 2 or abs(b.y - k.box.y) > k.box.h // 2 for k in kept
        ):
            kept.append(hit)
    return kept


def _place_of(places: list[_Place], hit: _Hit) -> _Place:
    b = hit.box
    for p in places:
        if p.template == hit.template and abs(p.x - b.x) <= b.w // 4 and abs(p.y - b.y) <= b.h // 2:
            return p
    places.append(_Place(hit.template, b.x, b.y, b.w, b.h))
    return places[-1]


def _union(boxes: Sequence[BBox], width: int, height: int) -> BBox:
    x1, y1 = min(b.x for b in boxes), min(b.y for b in boxes)
    x2 = min(width, max(b.x + b.w for b in boxes))
    y2 = min(height, max(b.y + b.h for b in boxes))
    return BBox(x=x1, y=y1, w=x2 - x1, h=y2 - y1)


def watermarks_of(
    per_frame: Sequence[Sequence[_Hit]], width: int, height: int, duration_ms: int
) -> tuple[Watermark, ...]:
    """The runs of frames each place is held for, as watermarks (see the module doc)."""
    places: list[_Place] = []
    for i, hits in enumerate(per_frame):
        for hit in hits:
            _place_of(places, hit).frames.append((i, hit.box))
    found = []
    for place in places:
        runs: list[list[tuple[int, BBox]]] = []
        for i, box in place.frames:
            if runs and i - runs[-1][-1][0] - 1 <= MAX_GAP:
                runs[-1].append((i, box))
            else:
                runs.append([(i, box)])
        for run in runs:
            first, last = run[0][0], run[-1][0]
            if last - first + 1 < MIN_RUN:
                continue
            start = first * FRAME_MS
            end = (last + 1) * FRAME_MS
            if duration_ms:
                end = min(end, duration_ms)
            if end <= start:
                continue
            region = _union([b for _, b in run], width, height)
            found.append(Watermark(region=region, start_ms=start, end_ms=end))
    return tuple(sorted(found, key=lambda w: (w.start_ms, w.region.x, w.region.y)))


def find(
    video,
    templates: Sequence[Template],
    subtitle_region: BBox | None,
    *,
    width: int,
    height: int,
    duration_ms: int,
    cancel: threading.Event | None = None,
    progress: Callable[[int], None] | None = None,
) -> tuple[Watermark, ...]:
    """The watermarks `templates` show as on `video`, which is `width`×`height` and
    lasts `duration_ms`. No template, nothing found."""
    found = find_in_frames(
        lambda: frames(video, width, height, cancel),
        templates,
        subtitle_region,
        height=height,
        width=width,
        duration_ms=duration_ms,
        progress=progress,
    )
    return () if cancel is not None and cancel.is_set() else found


def find_in_frames(
    open_frames: Callable[[], Iterable[np.ndarray]],
    templates: Sequence[Template],
    subtitle_region: BBox | None,
    *,
    width: int,
    height: int,
    duration_ms: int,
    progress: Callable[[int], None] | None = None,
) -> tuple[Watermark, ...]:
    """`find` over the frames `open_frames` yields, each time it is called: once to
    calibrate the sizes, once to search."""
    if not templates:
        return ()
    expected = max(1, duration_ms * SAMPLE_FPS // 1000)
    every = max(1, expected // CALIBRATION_FRAMES)
    scales = _calibrate(open_frames(), templates, height, every)
    variants = []
    for n, (template, kept) in enumerate(zip(templates, scales, strict=True)):
        for scale in kept:
            resized = _resized(template.image, scale)
            ink = int(np.count_nonzero(cv2.Canny(resized, CANNY_LOW, CANNY_HIGH)))
            variants.append(_Variant(n, edges(resized), ink))
    if not variants:
        return ()
    per_frame = []
    for i, frame in enumerate(open_frames()):
        per_frame.append(_search(frame, variants, subtitle_region))
        if progress is not None:
            progress(i)
    return watermarks_of(per_frame, width, height, duration_ms)
