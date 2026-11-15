"""The checks of a review that need no model: what can be measured is measured.

Each returns issues in the shape the workflow already routes on (`ReviewIssue`: the stage
to run again, the line, a reason), and each issue carries the score it counts against.
Pure: the activity supplies the measurements (the dub document, the loudness, the boxes
the subtitle detector saw on the source), so every check has a case that fails and one
that passes.

  localize  the line is empty, still has Chinese, or lost a number or Latin name of its source
  dub       a line still overruns its slot, is stretched to its limit, is cut by the end, or
            was read without some of its words
  captions  a caption is wider than its box
  render    the audio is off its loudness limits, or source subtitle shows outside its cover
"""

import itertools
import math
import re
import statistics
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, Protocol

from localization.agents.reviewer import Loudness
from localization.contracts import (
    BBox,
    DubSegmentsDocument,
    EditSpec,
    LocalizedSegment,
    ReviewIssue,
    SubtitleDocument,
)
from localization.contracts.models import STRETCH_MAX
from localization.text.glyphs import foreign_chars, foreign_chars_note
from localization.text.vi_numbers import missing_numbers

Dimension = Literal["fidelity", "timing", "visual"]

# A line is read too fast only at the fastest stretch the dubbing stage allows: every
# faster reading is already one the Dubber had to shorten, and one left at this limit is
# one it could not. Slower than SLOW_RATIO is near the other edge (STRETCH_MIN, 0.85).
# Either way the Dubber is asked again for that line.
FAST_RATIO = STRETCH_MAX
SLOW_RATIO = 0.88
# The assembled audio is normalised to the target; this far off means it was not.
LUFS_TOLERANCE = 2.0
PEAK_TOLERANCE = 0.5
# Left-over subtitle is judged on the boxes the subtitle detector drew on the source,
# one every `frame_interval_ms`, against the cover painted over them. A box is outside
# the cover when more than this share of it is uncovered: the model draws its box looser
# than the line, so one that only runs over an edge is the line under the cover.
RESIDUAL_UNCOVERED = 0.5
# The model also puts a box well away from the line on some frames, and on a third of
# them on one video: 70-90 px under a line that never moved from under the cover (checked
# on the source at 0.5, 2, 21 and 45 s), in and out between frames that boxed it in place.
# A burned-in line stays where it is while it shows, so a frame outside the cover counts
# only in a run of frames outside lasting RESIDUAL_MIN_MS: the frame with a box under the
# cover that breaks a shorter run saw the line there. Runs that long still come from the
# model, nine frames under a hidden line at most; on that video, 17% of the frames with a
# box; on another, 1%. A band the cover missed is reported once it is on this share of
# the frames with a box.
RESIDUAL_SHARE = Fraction(1, 3)
RESIDUAL_MIN_MS = 2000
# Two boxes are the same band when they overlap vertically by this much of the shorter.
RESIDUAL_SAME_ROW = 0.5
# How many of the frames that show the band the issue names.
RESIDUAL_TIMES_SHOWN = 4

_LATIN_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]+")


@dataclass(frozen=True)
class Finding:
    dimension: Dimension
    issue: ReviewIssue


def _found(dimension: Dimension, stage: str, reason: str, segment_id: int | None = None) -> Finding:
    return Finding(
        dimension,
        ReviewIssue(segment_id=segment_id, stage=stage, reason=reason[:500]),  # type: ignore[arg-type]
    )


# --- the lines ----------------------------------------------------------------------------


def line_findings(segments: Sequence[LocalizedSegment]) -> list[Finding]:
    """The cheap sanity of each line: something was written, in Vietnamese, with the
    numbers and Latin names its source has (spelled as digits or as words)."""
    found: list[Finding] = []
    for seg in segments:
        if seg.status == "unresolved":
            continue
        text = unicodedata.normalize("NFKC", seg.text_vi)
        src = unicodedata.normalize("NFKC", seg.src_zh)
        if sum(c.isalnum() for c in text) < 2:
            found.append(
                _found("fidelity", "localize", "the line is empty or has no words", seg.id)
            )
            continue
        if foreign := foreign_chars(text):
            found.append(
                _found(
                    "fidelity",
                    "localize",
                    f"the line still has characters that are not Vietnamese: "
                    f"{foreign_chars_note(foreign)}",
                    seg.id,
                )
            )
            continue
        lost = missing_numbers(src, text)
        names = [
            w for w in dict.fromkeys(_LATIN_NAME.findall(src)) if w.lower() not in text.lower()
        ]
        if lost or names:
            missing = ", ".join([*lost, *names])
            found.append(
                _found(
                    "fidelity",
                    "localize",
                    f"the source has {missing}, which the line does not carry",
                    seg.id,
                )
            )
    return found


# --- timing -------------------------------------------------------------------------------


def timing_findings(
    script: Sequence[LocalizedSegment],
    dub: DubSegmentsDocument,
    video_duration_ms: int,
    loudness: Loudness,
    *,
    target_lufs: float,
    true_peak_dbtp: float,
) -> list[Finding]:
    slots = {s.id: s for s in script}
    found: list[Finding] = []
    for seg in dub.segments:
        slot = slots.get(seg.id)
        if slot is None:
            continue
        if slot.status == "unresolved" and seg.status == "unresolved":
            continue
        spoken = (
            f"spoken {seg.duration_ms} ms in a {slot.slot_ms} ms slot, at x{seg.stretch_ratio:.2f}"
        )
        if seg.lost_words:
            found.append(
                _found(
                    "fidelity",
                    "dub",
                    "the voice left words out of the line: " + " ".join(seg.lost_words),
                    seg.id,
                )
            )
        if seg.overflow_unresolved:
            found.append(
                _found("timing", "dub", f"the line still overruns its slot: {spoken}", seg.id)
            )
        elif slot.start_ms + seg.duration_ms > video_duration_ms:
            found.append(
                _found(
                    "timing",
                    "dub",
                    "the line is cut off by the end of the video: it starts "
                    f"{max(0, video_duration_ms - slot.start_ms)} ms before the end and is "
                    f"{spoken}",
                    seg.id,
                )
            )
        elif seg.stretch_ratio >= FAST_RATIO:
            found.append(_found("timing", "dub", f"the line is read too fast: {spoken}", seg.id))
        elif seg.stretch_ratio <= SLOW_RATIO:
            found.append(_found("timing", "dub", f"the line is read too slow: {spoken}", seg.id))
    intentional_silence = bool(script) and all(s.status == "unresolved" for s in script)
    if not math.isfinite(loudness.lufs) and not intentional_silence:
        found.append(_found("timing", "render", "the audio of the video is silent"))
    elif math.isfinite(loudness.lufs) and abs(loudness.lufs - target_lufs) > LUFS_TOLERANCE:
        found.append(
            _found(
                "timing",
                "render",
                f"the audio is {loudness.lufs:.1f} LUFS, {abs(loudness.lufs - target_lufs):.1f} LU "
                f"from the {target_lufs:.1f} target",
            )
        )
    if loudness.true_peak_dbtp > true_peak_dbtp + PEAK_TOLERANCE:
        found.append(
            _found(
                "timing",
                "render",
                f"the audio peaks at {loudness.true_peak_dbtp:.1f} dBTP, over the "
                f"{true_peak_dbtp:.1f} limit",
            )
        )
    return found


# --- the picture --------------------------------------------------------------------------


class TextMeter(Protocol):
    def width(self, text: str, size: int) -> float: ...


def caption_findings(spec: EditSpec, meter: TextMeter) -> list[Finding]:
    """A caption line wider than the box it is centred in runs out of it (the layout
    only lets that happen to a single word wider than the box)."""
    found: list[Finding] = []
    for caption in spec.captions:
        widest = max(
            (meter.width(line, caption.font_px) for line in caption.text.split("\n")), default=0
        )
        if widest > spec.caption_box.w:
            found.append(
                _found(
                    "visual",
                    "captions",
                    f"the caption is {widest:.0f} px wide in a {spec.caption_box.w} px box",
                    caption.segment_id,
                )
            )
    return found


def _in_video_pixels(box: BBox, subtitles: SubtitleDocument, spec: EditSpec) -> BBox:
    """A box of the subtitle document (in the pixels of the source it was measured on)
    in the pixels of the spec's cover."""
    if (subtitles.width, subtitles.height) == (spec.width, spec.height):
        return box
    fx, fy = spec.width / subtitles.width, spec.height / subtitles.height
    return BBox(
        x=round(box.x * fx),
        y=round(box.y * fy),
        w=max(1, round(box.w * fx)),
        h=max(1, round(box.h * fy)),
    )


def _uncovered(box: BBox, cover: BBox) -> float:
    """The share of the box's area the cover does not reach."""
    across = max(0, min(box.x + box.w, cover.x + cover.w) - max(box.x, cover.x))
    down = max(0, min(box.y + box.h, cover.y + cover.h) - max(box.y, cover.y))
    return 1 - across * down / (box.w * box.h)


def _same_row(a: BBox, b: BBox) -> bool:
    shared = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    return shared >= RESIDUAL_SAME_ROW * min(a.h, b.h)


def _sustained(
    seen: Sequence[tuple[int, BBox]], cover: BBox, frames: int
) -> list[tuple[int, BBox]]:
    """The frames in runs of at least `frames` frames whose boxes are outside the cover.
    A frame with a box under the cover ends a run; one without a box does not (it saw
    nothing, or its answer was unusable)."""
    sustained: list[tuple[int, BBox]] = []
    for outside, run in itertools.groupby(
        seen, key=lambda frame: _uncovered(frame[1], cover) > RESIDUAL_UNCOVERED
    ):
        boxes = list(run)
        if outside and len(boxes) >= frames:
            sustained.extend(boxes)
    return sustained


def residual_findings(spec: EditSpec, subtitles: SubtitleDocument) -> list[Finding]:
    """Source subtitle the cover leaves visible: a band of the detector's boxes outside
    the cover for RESIDUAL_MIN_MS at a time, on enough frames to be text and not the
    model's error (see `RESIDUAL_SHARE`). Nothing to check without a cover, or in a
    document written before the detector kept its boxes."""
    interval = subtitles.frame_interval_ms
    if spec.cover is None or interval is None:
        return []
    cover = spec.cover.region
    # A frame without a box showed no text or had an unusable answer: it is not counted.
    seen = [
        (i, _in_video_pixels(box, subtitles, spec))
        for i, box in enumerate(subtitles.frame_boxes)
        if box is not None
    ]
    run = math.ceil(RESIDUAL_MIN_MS / interval)
    outside = _sustained(seen, cover, run)
    band = max(
        ([(i, box) for i, box in outside if _same_row(box, seed)] for _, seed in outside),
        key=len,
        default=[],
    )
    if not band or len(band) < max(run, RESIDUAL_SHARE * len(seen)):
        return []
    # One issue however many frames show it: it is one cover that missed the subtitle.
    boxes = [box for _, box in band]
    top = round(statistics.median(b.y for b in boxes))
    bottom = round(statistics.median(b.y + b.h for b in boxes))
    left = min(b.x for b in boxes)
    right = max(b.x + b.w for b in boxes)
    x, y, w, h = left, top, right - left, bottom - top
    at = ", ".join(str(i * interval) for i, _ in band[:RESIDUAL_TIMES_SHOWN])
    return [
        _found(
            "visual",
            "render",
            f"source text is burned in next to the caption (x={x} y={y} w={w} h={h}) at {at} ms: "
            "left-over source subtitle",
        )
    ]
