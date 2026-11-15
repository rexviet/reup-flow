"""What the review asks Jev and how it reads the answers."""

import pytest

from localization.agents import review_jev as rj
from localization.contracts import LocalizedSegment


def seg(i: int, zh: str = "打赏支持明镜与点点栏目", vi: str = "Ủng hộ kênh") -> LocalizedSegment:
    return LocalizedSegment(
        id=i, start_ms=0, end_ms=100, slot_ms=1000, src_zh=zh, text_vi=vi, on_screen=False
    )


def answers(segments: list[LocalizedSegment], **high: float) -> dict[str, float]:
    """Every question 0.05, except those named `<id>_<question>=p` in `high`."""
    out = {rj.question_name(s.id, q): 0.05 for s in segments for q in rj.QUESTION_TEXT}
    out.update({f"s{k}": v for k, v in high.items()})
    return out


def test_every_line_gets_the_three_questions_and_the_state_holds_the_lines_as_text() -> None:
    lines = [seg(0), seg(5, "转发", "Chia sẻ")]
    assert rj.build_state(lines) == {
        "segments": [
            {"id": 0, "source_zh": "打赏支持明镜与点点栏目", "target_vi": "Ủng hộ kênh"},
            {"id": 5, "source_zh": "转发", "target_vi": "Chia sẻ"},
        ]
    }
    questions = rj.build_questions(lines)
    assert set(questions) == {f"s{i}_{q}" for i in (0, 5) for q in rj.QUESTION_TEXT}
    assert "id is 5" in questions["s5_omits_content"].instructions


def test_a_line_whose_name_was_dropped_is_flagged_with_the_question_that_failed() -> None:
    lines = [seg(0), seg(1, "转发", "Chia sẻ")]
    result = rj.fidelity_issues(lines, answers(lines, **{"0_omits_content": 0.96}))
    (issue,) = result.issues
    assert (issue.segment_id, issue.stage) == (0, "localize")
    assert issue.reason.startswith("the Vietnamese leaves out a name, number or call to action")
    assert "96%" in issue.reason and "与点点栏目" in issue.reason and "Ủng hộ kênh" in issue.reason
    assert result.worst == 0.96


def test_the_question_that_failed_most_names_the_problem() -> None:
    lines = [seg(0)]
    got = answers(lines, **{"0_omits_content": 0.82, "0_changes_meaning": 0.93})
    (issue,) = rj.fidelity_issues(lines, got).issues
    assert "does not mean what the Chinese says" in issue.reason


def test_answers_just_under_the_threshold_flag_nothing() -> None:
    lines = [seg(0)]
    near = {q: rj.THRESHOLD - 0.01 for q in (rj.question_name(0, n) for n in rj.QUESTION_TEXT)}
    assert rj.fidelity_issues(lines, near).issues == []


def test_a_long_line_is_clipped_in_the_reason_to_fit_an_issue() -> None:
    lines = [seg(0, zh="你" * 600, vi="x " * 600)]
    (issue,) = rj.fidelity_issues(lines, answers(lines, **{"0_drops_clause": 0.9})).issues
    assert len(issue.reason) <= 500


def test_lines_are_split_into_calls_of_at_most_the_size_jev_was_measured_at() -> None:
    lines = [seg(i) for i in range(rj.SEGMENTS_PER_CALL + 1)]
    assert [len(c) for c in rj.chunks(lines)] == [rj.SEGMENTS_PER_CALL, 1]
    assert rj.chunks([]) == []


@pytest.mark.parametrize("missing", [rj.question_name(0, "omits_content")])
def test_an_answer_that_is_missing_is_a_failure_not_a_pass(missing: str) -> None:
    lines = [seg(0)]
    got = answers(lines)
    del got[missing]
    with pytest.raises(KeyError):
        rj.fidelity_issues(lines, got)
