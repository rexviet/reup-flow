"""Which stage a failed review sends the run back to: the earliest stage with a problem."""

from localization.contracts import ReviewIssue
from localization.workflows.review_routing import NoRoute, Route, route_issues


def issue(stage: str, reason: str = "r", segment_id: int | None = None) -> ReviewIssue:
    return ReviewIssue(stage=stage, reason=reason, segment_id=segment_id)  # type: ignore[arg-type]


def test_a_translation_problem_wins_over_everything_after_it() -> None:
    route = route_issues((issue("dub", "x", 1), issue("localize", "y", 0), issue("captions")))
    assert isinstance(route, Route) and route.rerun == "localize"
    assert route.notes == ("segment 1: x", "segment 0: y", "r")


def test_a_reading_problem_names_the_lines_to_run_again_once_each_in_order() -> None:
    route = route_issues((issue("dub", "a", 3), issue("dub", "b", 1), issue("dub", "c", 3)))
    assert isinstance(route, Route)
    assert (route.rerun, route.segment_ids) == ("dub", (1, 3))


def test_a_layout_problem_runs_the_layout_again() -> None:
    route = route_issues((issue("captions", "cut off"),))
    assert isinstance(route, Route) and route.rerun == "captions" and route.segment_ids == ()


def test_a_reading_problem_with_no_line_named_has_no_route() -> None:
    route = route_issues((issue("dub", "bad"), issue("dub", "worse", 2)))
    assert isinstance(route, NoRoute) and "named no segment" in route.reason


def test_a_reading_problem_is_still_routed_when_another_issue_is_not_fixable() -> None:
    route = route_issues((issue("render"), issue("dub", "x", 2)))
    assert isinstance(route, Route) and route.rerun == "dub"


def test_problems_only_in_stages_a_rerun_cannot_fix_have_no_route() -> None:
    for stage in ("render", "transcribe", "detect_subtitles"):
        route = route_issues((issue(stage),))
        assert isinstance(route, NoRoute) and stage in route.reason


def test_a_long_note_is_cut_to_what_the_localizer_accepts() -> None:
    route = route_issues((issue("localize", "x" * 500, 12),))
    assert isinstance(route, Route) and all(len(n) <= 500 for n in route.notes)
