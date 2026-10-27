"""What the review asks Jev about the translation, and what its answers mean.

Jev answers each Noul question with a probability that its statement is true. It does
not do arithmetic (it gave ~0.5 for sums), so everything that can be computed is checked
in `review_checks` and only what needs understanding two languages is asked here:

  omits_content    the Vietnamese leaves out a name, a number or a call to action
  drops_clause     the Vietnamese lacks a whole clause or sentence of the Chinese
  changes_meaning  the Vietnamese says something else (wrong action, wrong subject, not
                   Vietnamese)

Thresholds live here, in our code. Measured on real lines through OpenRouter (the real
dropped `与点点栏目`, a line with the name kept, long lines with a clause cut, wrong
actions, untranslated Chinese): faithful lines stayed under 0.75 on every question and
the defective ones were at 0.85 or more on at least one, with the same answer to two
decimals across repeats. More questions in one call dilute that, so a call carries at
most `SEGMENTS_PER_CALL` lines.

The text of the lines is untrusted (it is taken from the video). Jev's output is only
probabilities, so there is nothing in it for an injected instruction to steer except the
three numbers.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from typesafe_sdk import Noul

from localization.contracts import LocalizedSegment, ReviewIssue

SEGMENTS_PER_CALL = 12
THRESHOLD = 0.8
REASON_LINE_CHARS = 90

QUESTION_TEXT = {
    "omits_content": (
        "`target_vi` leaves out something that `source_zh` says: a person, channel, show, "
        "brand or column name, a number, or a call to action (like, subscribe, share, "
        "donate).",
        "Something named or asked for in source_zh is missing from target_vi.",
        "Every name, number and call to action in source_zh appears in target_vi, "
        "translated or transliterated.",
    ),
    "drops_clause": (
        "Walk through `source_zh` clause by clause. `target_vi` is missing a whole clause "
        "or sentence of it, even if what remains reads well.",
        "At least one clause of source_zh has no counterpart in target_vi.",
        "Every clause of source_zh has a counterpart in target_vi.",
    ),
    "changes_meaning": (
        "`target_vi` says something different from `source_zh`: a wrong action, wrong "
        "number, wrong subject, or text that is not Vietnamese.",
        "target_vi means something other than source_zh.",
        "target_vi means the same as source_zh.",
    ),
}

# What the Localizer is told, per question that failed.
REASON = {
    "omits_content": "the Vietnamese leaves out a name, number or call to action of the Chinese",
    "drops_clause": "the Vietnamese lacks a whole clause or sentence of the Chinese",
    "changes_meaning": "the Vietnamese does not mean what the Chinese says",
}


def question_name(segment_id: int, question: str) -> str:
    return f"s{segment_id}_{question}"


def build_state(segments: Sequence[LocalizedSegment]) -> dict[str, object]:
    return {
        "segments": [{"id": s.id, "source_zh": s.src_zh, "target_vi": s.text_vi} for s in segments]
    }


def build_questions(segments: Sequence[LocalizedSegment]) -> dict[str, Noul]:
    questions: dict[str, Noul] = {}
    for seg in segments:
        for name, (ask, yes, no) in QUESTION_TEXT.items():
            questions[question_name(seg.id, name)] = Noul(
                instructions=f"For the item of `segments` whose id is {seg.id}: {ask}",
                criteria={"true": yes, "false": no},
            )
    return questions


def chunks(segments: Sequence[LocalizedSegment]) -> list[Sequence[LocalizedSegment]]:
    return [segments[i : i + SEGMENTS_PER_CALL] for i in range(0, len(segments), SEGMENTS_PER_CALL)]


def _clip(text: str) -> str:
    one_line = " ".join(text.split())
    return (
        one_line if len(one_line) <= REASON_LINE_CHARS else one_line[: REASON_LINE_CHARS - 1] + "…"
    )


@dataclass(frozen=True)
class Verdicts:
    issues: list[ReviewIssue]
    # The highest probability of any question for any line, for the log.
    worst: float


def fidelity_issues(segments: Iterable[LocalizedSegment], answers: dict[str, float]) -> Verdicts:
    """One issue per line whose answers cross the threshold, naming the question that
    failed most. The Localizer repairs against its own source, so the reason says what
    kind of problem it is and shows the two lines, no more."""
    issues: list[ReviewIssue] = []
    worst = 0.0
    for seg in segments:
        scored = {q: answers[question_name(seg.id, q)] for q in QUESTION_TEXT}
        worst = max(worst, *scored.values())
        failed = max(scored, key=lambda q: scored[q])
        if scored[failed] >= THRESHOLD:
            issues.append(
                ReviewIssue(
                    segment_id=seg.id,
                    stage="localize",
                    reason=(
                        f"{REASON[failed]} (Jev {scored[failed]:.0%}); "
                        f"zh: {_clip(seg.src_zh)} | vi: {_clip(seg.text_vi)}"
                    ),
                )
            )
    return Verdicts(issues, worst)
