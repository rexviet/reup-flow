"""The review is made on what the render is made from (the script, the dubbed track and the
edit spec), so each round is laid out and reviewed without a render, and the video is
rendered once, after the last round, whatever the verdict.

A reviewer that looks at frames (the `llm` mode) answers, asked before the render, that it
needs one: that round is rendered and reviewed on the render, as every round was before."""

import json

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from tests.support.review_fakes import ReviewFakes, issue, verdict
from tests.workflows.test_review_loop import OUTPUT, REVIEW, Run, row, run_workflow


def after(names: list[str], first: str) -> list[str]:
    return names[names.index(first) :]


async def review_inputs(run: Run, name: str) -> list[list[dict]]:
    """The arguments each `name` call was scheduled with, read from the history."""
    history = await run.handle.fetch_history()
    return [
        [json.loads(p.data) for p in e.activity_task_scheduled_event_attributes.input.payloads]
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_type.name == name
    ]


async def test_a_run_that_passes_is_reviewed_then_rendered_once_then_published(
    temporal_client: Client,
) -> None:
    run = await run_workflow(temporal_client, Run())
    assert run.result.publish.outcome == "published"
    assert after(await run.scheduled(), "layout_captions") == [
        "layout_captions",
        "review_before_render",
        "render",
        "publish",
    ]
    assert run.reviewing.calls["render"] == 1
    assert run.reviewing.calls["review_render"] == 0


async def test_the_review_is_given_no_render(temporal_client: Client) -> None:
    run = await run_workflow(temporal_client, Run())
    (args,) = await review_inputs(run, "review_before_render")
    # spec, script, dubbed track, edit spec, round: nothing a render answers with.
    assert len(args) == 5
    assert not any("render_key" in a for a in args if isinstance(a, dict))
    assert run.reviewing.reviews[0]["render_key"] is None
    # What it reads the loudness from is the dubbed track the render puts in the video.
    assert run.reviewing.reviews[0]["audio_key"] == run.result.dub.audio_key


async def test_a_two_round_run_renders_once_after_the_last_review(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[verdict(issue("dub", "read too fast", segment_id=1)), verdict()]
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    names = await run.scheduled()
    assert names.count("review_before_render") == 2
    assert names.count("render") == 1
    assert names.index("render") > max(
        i for i, n in enumerate(names) if n == "review_before_render"
    )
    assert after(names, "render") == ["render", "publish"]
    # The video rendered is the one of the second round, the one the last review saw.
    last = run.reviewing.reviews[-1]
    assert last["round"] == 1 and last["spec_sha"] == run.reviewing.layouts[1]["sha"]
    assert run.reviewing.store.get_bytes(OUTPUT, produced_by="test") == b"mp4:" + (
        last["spec_sha"].encode()
    )


async def test_a_run_that_fails_every_review_still_renders_once_for_the_person(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[
            verdict(issue("dub", "read too fast", segment_id=1)),
            verdict(issue("dub", "still too fast", segment_id=1)),
        ]
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "needs_human"
    assert run.reviewing.calls["review_before_render"] == 2
    assert run.reviewing.calls["render"] == 1
    assert run.reviewing.store.get_bytes(REVIEW, produced_by="test") == b"mp4:" + (
        run.reviewing.layouts[1]["sha"].encode()
    )


async def test_a_review_that_cannot_be_made_still_renders_the_video_for_a_person(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        fail_on={
            ("review_before_render", 1): ApplicationError(
                "no usable answer", type="UnexpectedModelBehavior", non_retryable=True
            )
        }
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "needs_human"
    assert run.reviewing.calls["render"] == 1
    assert row(run)["record"]["verdict"] is None
    assert run.reviewing.store.get_bytes(REVIEW, produced_by="test") == b"mp4:" + (
        run.reviewing.layouts[0]["sha"].encode()
    )


async def test_a_render_that_fails_for_good_after_a_passed_review_fails_the_run_with_its_reason(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        fail_on={
            ("render", 1): ApplicationError(
                "the render ran past its deadline", type="RenderDeadline", non_retryable=True
            )
        }
    )
    await run_workflow(temporal_client, run)
    assert isinstance(run.outcome, BaseException)
    assert run.reviewing.calls["review_before_render"] == 1
    assert run.reviewing.calls["render"] == 1
    assert not run.has(OUTPUT) and not run.has(REVIEW)
    assert row(run)["status"] == "failed" and row(run)["review_rounds"] == 1
    (reason,) = row(run)["record"]["reasons"]
    assert reason.startswith("RenderDeadline: ")


async def test_a_reviewer_that_needs_frames_is_shown_the_render_of_each_round(
    temporal_client: Client,
) -> None:
    """The vision reviewer cannot judge a video that does not exist yet: each round is
    rendered and reviewed on its render, and the last render is not made a second time."""
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[verdict(issue("dub", "read too fast", segment_id=1)), verdict()],
        needs_render=True,
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    names = await run.scheduled()
    assert after(names, "layout_captions")[:4] == [
        "layout_captions",
        "review_before_render",
        "render",
        "review_render",
    ]
    assert after(names, "review_render")[-2:] == ["review_render", "publish"]
    assert names.count("render") == names.count("review_render") == 2
    assert [r["render_key"] is not None for r in run.reviewing.reviews] == [True, True]
    assert run.reviewing.store.get_bytes(OUTPUT, produced_by="test") == b"mp4:" + (
        run.reviewing.layouts[1]["sha"].encode()
    )
