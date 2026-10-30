"""A failed review sends the run back only when a rerun could fix what it found.

The shape of the first case is a recorded run: three problems, all on the last line,
which starts 402 ms before an 11133 ms video ends and has the slot floor (1000 ms)."""

from temporalio.client import Client

from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.support.review_fakes import ReviewFakes, issue, verdict
from tests.support.three_review_flow import ThreeReviewWorkflow
from tests.workflows.test_review_loop import REVIEW, Run, row, run_workflow

LAST_LINE = {1: (402, 1000)}


def last_line_problems():
    return verdict(
        issue("localize", "the call to support the channel lost its meaning", segment_id=1),
        issue("dub", "1784 ms of speech in a 1000 ms slot", segment_id=1),
        issue("captions", "the caption is visible about 0.3 s", segment_id=1),
    )


async def test_problems_only_on_a_line_with_no_time_end_after_one_round(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[last_line_problems(), last_line_problems()], tight=LAST_LINE
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "needs_human"
    assert run.reviewing.calls["review_before_render"] == 1
    assert run.reviewing.calls["render"] == 1
    assert run.reviewing.calls["layout_captions"] == 1
    # Neither the Localizer nor the Dubber ran again, and no second track was made.
    assert run.localizing.model.answer_attempts == 1
    assert run.localizing.calls["assemble_script"] == 1
    assert run.dubbing.calls["write_dubbed_script"] == 1
    assert run.dubbing.calls["assemble_audio"] == 1
    assert run.dubbing.ports.spoken == []


async def test_the_run_keeps_its_render_for_a_person_and_says_why(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(verdicts=[last_line_problems()], tight=LAST_LINE)
    await run_workflow(temporal_client, run)
    assert run.result.publish.review_key == REVIEW and run.has(REVIEW)
    assert row(run)["status"] == "needs_human" and row(run)["review_rounds"] == 1
    reasons = " ".join(row(run)["record"]["reasons"])
    assert "starts 402 ms before the video ends" in reasons
    assert "a rerun cannot make it fit" in reasons


async def test_a_line_in_the_middle_with_a_gap_after_it_is_still_run_again_once(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[verdict(issue("dub", "overruns its slot", segment_id=0)), verdict()],
        tight={1: (402, 1000)},
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    assert run.reviewing.calls["review_before_render"] == 2
    assert run.dubbing.calls["assemble_audio"] == 2
    assert run.dubbing.rounds_seen == [0, 1]


async def test_a_mix_of_a_hopeless_line_and_a_fixable_one_runs_only_what_can_help(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[
            verdict(
                issue("localize", "meaning lost", segment_id=1),
                issue("dub", "rushed", segment_id=0),
            ),
            verdict(),
        ],
        tight=LAST_LINE,
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    # The Localizer was not run again for the line nothing could save.
    assert run.localizing.calls["assemble_script"] == 1
    assert [d.id for d in run.dubbing.decisions_seen[1].items] == [0]


async def test_the_same_problem_on_the_same_line_after_a_rerun_stops_the_rounds(
    temporal_client: Client,
) -> None:
    run = Run()
    again = verdict(issue("dub", "still rushed", segment_id=0))
    run.reviewing = ReviewFakes(verdicts=[again, again, again])
    await run_workflow(temporal_client, run, workflow=ThreeReviewWorkflow)
    assert run.reviewing.calls["review_before_render"] == 2
    assert run.dubbing.calls["assemble_audio"] == 2
    assert run.result.publish.outcome == "needs_human"
    assert "came back after a rerun" in " ".join(row(run)["record"]["reasons"])


async def test_a_different_problem_after_a_rerun_is_run_again_when_rounds_allow(
    temporal_client: Client,
) -> None:
    """The control for the test above: with the same room for a third round, it is the
    repetition that stops the run and not the limit."""
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[
            verdict(issue("dub", "rushed", segment_id=0)),
            verdict(issue("dub", "rushed", segment_id=1)),
            verdict(),
        ]
    )
    await run_workflow(temporal_client, run, workflow=ThreeReviewWorkflow)
    assert run.reviewing.calls["review_before_render"] == 3
    assert run.result.publish.outcome == "published"


def test_the_workflow_keeps_the_two_round_limit() -> None:
    assert VideoLocalizationWorkflow._review_rounds == 2  # noqa: SLF001
