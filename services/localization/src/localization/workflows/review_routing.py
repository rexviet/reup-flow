"""Which stage a failed review sends the run back to. Pure: the workflow asks, the
answer depends on the verdict and nothing else.

The rule is "run again the earliest stage that has a problem, and nothing before it":

  localize   the translation is wrong -> the Localizer again with the reviewer's notes,
             then everything after it (the dubbing, the captions, the render)
  dub        a named line is read badly -> the Dubber again on exactly those lines,
             then the new track, the captions and the render
  captions   the layout is wrong -> the layout again, then the render

Anything else (a broken picture, a stage before the Localizer, a line that is not
named) is not something a rerun here can fix: the run goes to a person.

A rerun also cannot fix a line that has no time: one that starts so close to the end of
the video that it cannot be spoken or read there however it is written, or one that
already came back after a rerun was asked to fix it. `plan_rerun` drops those and sends
the run to a person when nothing else is left.
"""

from collections.abc import Iterable

from localization.contracts import ReviewIssue, SegmentRoom
from localization.contracts.base import Contract

NOTE_MAX = 500

# A line with less than this to the end of the video, or a slot shorter than this, is not
# something a rerun can make fit. The slot is never below 1000 ms, so near the end of the
# video it promises time the video does not have (the recorded run that set this limit
# had a line with 400 ms left and a slot of 1000 ms).
UNFIXABLE_ROOM_MS = 1500

# What a rerun was asked to fix: the stage and the line (None: no line named).
IssueKey = tuple[str, int | None]


class Route(Contract):
    # "localize", "dub" or "captions".
    rerun: str
    # The segments a `dub` rerun is about; empty for the others.
    segment_ids: tuple[int, ...]
    # What the reviewer said, for the stage that runs again.
    notes: tuple[str, ...]


class NoRoute(Contract):
    reason: str


def _note(issue: ReviewIssue) -> str:
    where = f"segment {issue.segment_id}: " if issue.segment_id is not None else ""
    return f"{where}{issue.reason}"[:NOTE_MAX]


def route_issues(issues: tuple[ReviewIssue, ...]) -> Route | NoRoute:
    stages = {issue.stage for issue in issues}
    notes = tuple(_note(i) for i in issues if i.stage in ("localize", "dub", "captions"))
    if "localize" in stages:
        return Route(rerun="localize", segment_ids=(), notes=notes)
    if "dub" in stages:
        dub = [i for i in issues if i.stage == "dub"]
        if any(i.segment_id is None for i in dub):
            return NoRoute(
                reason="the reviewer found a problem with the reading but named no segment"
            )
        ids = tuple(sorted({i.segment_id for i in dub if i.segment_id is not None}))
        return Route(rerun="dub", segment_ids=ids, notes=notes)
    if "captions" in stages:
        return Route(rerun="captions", segment_ids=(), notes=notes)
    return NoRoute(
        reason="the reviewer's problems are in "
        + ", ".join(sorted(stages) or ["no stage"])
        + ", which a rerun here cannot fix"
    )


def issue_key(issue: ReviewIssue) -> IssueKey:
    return (issue.stage, issue.segment_id)


def _why_no_room(issue: ReviewIssue, room: SegmentRoom) -> str | None:
    if room.to_end_ms < UNFIXABLE_ROOM_MS:
        return (
            f"the line of segment {room.id} starts {room.to_end_ms} ms before the video ends; "
            "a rerun cannot make it fit"
        )
    if room.slot_ms < UNFIXABLE_ROOM_MS:
        return (
            f"the line of segment {room.id} has a slot of {room.slot_ms} ms; "
            "a rerun cannot make it fit"
        )
    return None


def plan_rerun(
    issues: tuple[ReviewIssue, ...],
    rooms: Iterable[SegmentRoom],
    asked_before: frozenset[IssueKey],
) -> Route | NoRoute:
    """`route_issues` over the issues a rerun can fix. An issue that names a line with
    no time left, or one a rerun was already asked to fix, is left out; when every issue
    is left out the answer says why, so the run goes to a person without another round."""
    by_id = {r.id: r for r in rooms}
    fixable: list[ReviewIssue] = []
    hopeless: list[str] = []
    for item in issues:
        room = by_id.get(item.segment_id) if item.segment_id is not None else None
        why = _why_no_room(item, room) if room is not None else None
        if why is None and issue_key(item) in asked_before:
            where = f" of segment {item.segment_id}" if item.segment_id is not None else ""
            why = (
                f"the {item.stage} problem{where} came back after a rerun; another would not fix it"
            )
        if why is None:
            fixable.append(item)
        else:
            hopeless.append(why)
    if not fixable and hopeless:
        return NoRoute(reason="; ".join(dict.fromkeys(hopeless)))
    return route_issues(tuple(fixable))
