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
"""

from localization.contracts import ReviewIssue
from localization.contracts.base import Contract

NOTE_MAX = 500


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
