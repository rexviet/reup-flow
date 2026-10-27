"""The time a line has to be spoken in.

Speech runs into the silence after a segment, and forced alignment sometimes
collapses a segment to a few milliseconds (music under the words). So the slot
offered for a line is not the segment's own span: it extends through the quiet up to
the start of the next segment, by at most `MAX_EXTENSION_MS`, and is never shorter
than `MIN_SLOT_MS`. The last segment extends towards the end of the video.

One function, used both for the slot the model is shown and for the slot stored in
the script, so they cannot disagree.
"""

from collections.abc import Sequence

# How far past its own end a segment may borrow from the silence that follows.
MAX_EXTENSION_MS = 1500
# A few syllables at a normal speaking rate: no line is asked to fit in less.
MIN_SLOT_MS = 1000


def slots_ms(spans: Sequence[tuple[int, int]], duration_ms: int | None = None) -> list[int]:
    """`spans` are `(start_ms, end_ms)` in spoken order. Returns one slot per span."""
    out: list[int] = []
    for i, (start, end) in enumerate(spans):
        following = spans[i + 1][0] if i + 1 < len(spans) else duration_ms
        limit = end + MAX_EXTENSION_MS
        if following is not None:
            limit = min(limit, max(following, end))
        out.append(max(MIN_SLOT_MS, limit - start))
    return out
