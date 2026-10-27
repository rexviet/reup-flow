"""The checks of a review that need no model: what can be measured is measured.

Each returns issues in the shape the workflow already routes on (`ReviewIssue`: the stage
to run again, the line, a reason), and each issue carries the score it counts against.
Pure: the activity supplies the measurements (the dub document, the loudness, what the
OCR found), so every check has a case that fails and one that passes.

  localize  the line is empty, still has Chinese, or lost a number or Latin name of its source
  dub       a line still overruns its slot, is stretched to its limit, or is cut by the end
  captions  a caption is wider than its box
  render    the audio is off its loudness limits, or source text shows next to the caption
"""

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from localization.agents.reviewer import Loudness, ResidualFinding
from localization.contracts import (
    DubSegmentsDocument,
    EditSpec,
    LocalizedSegment,
    ReviewIssue,
)
from localization.text.glyphs import foreign_chars, foreign_chars_note
from localization.text.vi_syllables import numbers_to_words

Dimension = Literal["fidelity", "timing", "visual"]

# A line played faster or slower than this is at the edge of what the stretch can do
# (it is held between 0.85 and 1.15): the Dubber is asked again for that line.
FAST_RATIO = 1.12
SLOW_RATIO = 0.88
# The assembled audio is normalised to the target; this far off means it was not.
LUFS_TOLERANCE = 2.0
PEAK_TOLERANCE = 0.5
# Text found outside the caption's place counts as left-over subtitle only when it is
# this close to it, in heights of the band itself: a logo or a title elsewhere on the
# picture is the video's own.
RESIDUAL_DISTANCE = 2.0
RESIDUAL_FRAMES = 4

_LATIN_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_NUMBER = re.compile(r"\d+")


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


def _digits_kept(source_number: str, target: str) -> bool:
    plain = re.sub(r"(?<=\d)[.,\s](?=\d)", "", target)
    return source_number in plain or numbers_to_words(source_number).lower() in target.lower()


def line_findings(segments: Sequence[LocalizedSegment]) -> list[Finding]:
    """The cheap sanity of each line: something was written, in Vietnamese, with the
    numbers and Latin names its source has (spelled as digits or as words)."""
    found: list[Finding] = []
    for seg in segments:
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
        lost = [n for n in dict.fromkeys(_NUMBER.findall(src)) if not _digits_kept(n, text)]
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
        spoken = (
            f"spoken {seg.duration_ms} ms in a {slot.slot_ms} ms slot, at x{seg.stretch_ratio:.2f}"
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
    if not math.isfinite(loudness.lufs):
        found.append(_found("timing", "render", "the audio of the video is silent"))
    elif abs(loudness.lufs - target_lufs) > LUFS_TOLERANCE:
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


def residual_times(spec: EditSpec) -> list[int]:
    """The frames to look at for left-over subtitle: the middle of up to
    `RESIDUAL_FRAMES` captions spread over the video, or evenly spaced when there are
    none. None when no subtitle was covered: there is nothing that could be left over."""
    if spec.cover is None:
        return []
    captions = spec.captions
    if captions:
        step = max(1, math.ceil(len(captions) / RESIDUAL_FRAMES))
        return [(c.start_ms + c.end_ms) // 2 for c in captions[::step]][:RESIDUAL_FRAMES]
    last = spec.duration_ms - 1
    return [
        min(last, spec.duration_ms * (2 * i + 1) // (2 * RESIDUAL_FRAMES))
        for i in range(RESIDUAL_FRAMES)
    ]


def _gap(box: tuple[int, int, int, int], place: tuple[int, int, int, int]) -> float:
    """Pixels between two boxes along the vertical, 0 when they overlap on it."""
    _, y, _, h = box
    _, py, _, ph = place
    return max(0, max(y, py) - min(y + h, py + ph))


def residual_findings(spec: EditSpec, found: Sequence[ResidualFinding]) -> list[Finding]:
    places = [(spec.caption_box.x, spec.caption_box.y, spec.caption_box.w, spec.caption_box.h)]
    if spec.cover is not None:
        c = spec.cover.region
        places.append((c.x, c.y, c.w, c.h))
    hits: list[ResidualFinding] = []
    for item in found:
        if item.unavailable or item.box is None:
            continue
        x, y, w, h = item.box
        # Inside the place of the caption is the caption itself.
        if any(
            x >= px - 0.15 * pw
            and y >= py - 0.15 * ph
            and x + w <= px + 1.15 * pw
            and y + h <= py + 1.15 * ph
            for px, py, pw, ph in places
        ):
            continue
        if min(_gap(item.box, p) for p in places) <= RESIDUAL_DISTANCE * h:
            hits.append(item)
    if not hits:
        return []
    # One issue however many frames show it: it is one cover that missed the subtitle.
    x, y, w, h = hits[0].box  # type: ignore[misc]
    at = ", ".join(str(i.at_ms) for i in hits)
    return [
        _found(
            "visual",
            "render",
            f"source text is burned in next to the caption (x={x} y={y} w={w} h={h}) at {at} ms: "
            "left-over source subtitle",
        )
    ]
