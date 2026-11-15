"""Which stage a failed review sends the run back to: the earliest stage with a problem."""

from localization.contracts import ReviewIssue, SegmentRoom
from localization.workflows.review_routing import NoRoute, Route, plan_rerun, route_issues


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


# --- a line a rerun cannot fix -----------------------------------------------------------


def room(id_: int, to_end_ms: int = 20_000, slot_ms: int = 3000) -> SegmentRoom:
    return SegmentRoom(id=id_, to_end_ms=to_end_ms, slot_ms=slot_ms)


def test_a_line_with_time_to_spare_is_routed_as_before() -> None:
    route = plan_rerun((issue("dub", "x", 1),), [room(1)], frozenset())
    assert isinstance(route, Route) and (route.rerun, route.segment_ids) == ("dub", (1,))


def test_the_limit_is_on_the_time_to_the_end_of_the_video() -> None:
    just_under = plan_rerun((issue("dub", "x", 1),), [room(1, to_end_ms=1499)], frozenset())
    exactly = plan_rerun((issue("dub", "x", 1),), [room(1, to_end_ms=1500)], frozenset())
    assert isinstance(just_under, NoRoute) and isinstance(exactly, Route)
    assert "starts 1499 ms before the video ends" in just_under.reason
    assert "a rerun cannot make it fit" in just_under.reason


def test_the_limit_is_on_the_slot_too() -> None:
    just_under = plan_rerun((issue("dub", "x", 1),), [room(1, slot_ms=1499)], frozenset())
    exactly = plan_rerun((issue("dub", "x", 1),), [room(1, slot_ms=1500)], frozenset())
    assert isinstance(just_under, NoRoute) and isinstance(exactly, Route)
    assert "slot of 1499 ms" in just_under.reason


def test_every_stage_a_rerun_could_run_is_hopeless_for_a_line_with_no_time() -> None:
    tight = [room(2, to_end_ms=400, slot_ms=1000)]
    for stage in ("localize", "dub", "captions"):
        route = plan_rerun((issue(stage, "x", 2),), tight, frozenset())
        assert isinstance(route, NoRoute), stage


def test_one_line_with_time_left_keeps_the_rerun_and_leaves_the_hopeless_line_out() -> None:
    """The translation problem is on the last line, which no rewrite can save; the
    reading problem is in the middle. Only the reading is run again, and the Localizer
    (the earliest stage named) is not."""
    tight = room(2, to_end_ms=400, slot_ms=1000)
    route = plan_rerun(
        (issue("localize", "meaning lost", 2), issue("dub", "rushed", 1)),
        [room(1), tight],
        frozenset(),
    )
    assert isinstance(route, Route) and route.rerun == "dub"
    assert route.segment_ids == (1,) and route.notes == ("segment 1: rushed",)


def test_an_issue_that_names_no_line_is_never_called_hopeless_for_want_of_time() -> None:
    route = plan_rerun((issue("captions", "cut off"),), [room(0, to_end_ms=10)], frozenset())
    assert isinstance(route, Route) and route.rerun == "captions"


def test_the_same_problem_on_the_same_line_after_a_rerun_is_not_run_again() -> None:
    asked = frozenset({("dub", 1)})
    again = plan_rerun((issue("dub", "still rushed", 1),), [room(1)], asked)
    other_line = plan_rerun((issue("dub", "rushed", 2),), [room(2)], asked)
    other_stage = plan_rerun((issue("localize", "wrong", 1),), [room(1)], asked)
    assert isinstance(again, NoRoute) and "came back after a rerun" in again.reason
    assert isinstance(other_line, Route) and isinstance(other_stage, Route)


def test_a_problem_that_names_no_line_can_come_back_too() -> None:
    route = plan_rerun((issue("captions", "cut off"),), [], frozenset({("captions", None)}))
    assert isinstance(route, NoRoute) and "came back after a rerun" in route.reason


def test_the_reasons_of_every_hopeless_issue_are_given_once_each() -> None:
    tight = [room(3, to_end_ms=402, slot_ms=1000)]
    route = plan_rerun(
        (issue("localize", "a", 3), issue("dub", "b", 3), issue("captions", "c", 3)),
        tight,
        frozenset(),
    )
    assert isinstance(route, NoRoute)
    assert route.reason.count("starts 402 ms before the video ends") == 1
