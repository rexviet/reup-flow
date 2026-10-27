"""Layout, render, review and publish inside the real workflow: which stage a failed
review sends the run back to and what is NOT run again, what ends in `output/` and what
in `review/`, and how a stage that fails for good is left on the run.

Activities are stand-ins with the real shapes, except publishing, which is the real
activity over an object store on a directory. The Localizer and the Dubber are the real
agents on scripted models."""

import asyncio
import uuid

import pytest
from botocore.exceptions import ClientError
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from localization.contracts import LocalizationResult
from localization.storage.object_store import output_key, review_key
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.support.dub_fakes import DubFakes
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.review_fakes import ReviewFakes, issue, verdict

OUTPUT = output_key("7001")
REVIEW = review_key("7001", 1)


class Run:
    """Everything a test wants to look at after one run of the workflow."""

    def __init__(self) -> None:
        self.prep = PrepFakes()
        self.localizing = LocalizerFakes()
        self.dubbing = DubFakes()
        self.reviewing = ReviewFakes()
        self.handle = None
        self.outcome: LocalizationResult | BaseException | None = None

    @property
    def result(self) -> LocalizationResult:
        assert isinstance(self.outcome, LocalizationResult), self.outcome
        return self.outcome

    def has(self, key: str) -> bool:
        return self.reviewing.store.exists(key)

    async def scheduled(self) -> list[str]:
        history = await self.handle.fetch_history()
        return [
            e.activity_task_scheduled_event_attributes.activity_type.name
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        ]


async def run_workflow(
    client: Client, run: Run, *, timeout: float = 60, workflow: type = VideoLocalizationWorkflow
) -> Run:
    async with prep_workers(
        client,
        run.prep,
        run.localizing,
        dubbing=run.dubbing,
        reviewing=run.reviewing,
        workflow=workflow,
    ):
        run.handle = await client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        try:
            run.outcome = await asyncio.wait_for(run.handle.result(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            run.outcome = exc
    return run


def row(run: Run) -> dict:
    return run.reviewing.runs.rows[("7001", 1)]


# --- a review that passes ---------------------------------------------------------------


async def test_a_run_that_passes_review_is_published_to_output(temporal_client: Client) -> None:
    run = await run_workflow(temporal_client, Run())
    assert run.result.publish is not None
    assert (run.result.publish.outcome, run.result.publish.output_key) == ("published", OUTPUT)
    assert run.has(OUTPUT) and not run.has(REVIEW)
    assert row(run)["status"] == "published" and row(run)["output"] == OUTPUT
    assert row(run)["needs_human"] is False and row(run)["review_rounds"] == 1


async def test_the_published_video_is_the_one_that_was_reviewed(temporal_client: Client) -> None:
    run = await run_workflow(temporal_client, Run())
    reviewed = run.reviewing.reviews[0]["render_key"]
    assert run.reviewing.store.get_bytes(OUTPUT, produced_by="test") == (
        run.reviewing.store.get_bytes(reviewed, produced_by="test")
    )


async def test_after_the_track_the_steps_run_in_order_on_their_own_queues(
    temporal_client: Client,
) -> None:
    run = await run_workflow(temporal_client, Run())
    names = await run.scheduled()
    assert names[names.index("assemble_audio") :] == [
        "assemble_audio",
        "layout_captions",
        "render",
        "review_render",
        "publish",
    ]
    history = await run.handle.fetch_history()
    queue = {
        e.activity_task_scheduled_event_attributes.activity_type.name: (
            e.activity_task_scheduled_event_attributes.task_queue.name
        )
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    }
    assert queue["layout_captions"] == "media"
    assert queue["render"] == "render"
    assert queue["review_render"] == "llm"
    assert queue["publish"] == "io"


async def test_the_captions_and_the_review_use_the_script_the_track_was_built_from(
    temporal_client: Client,
) -> None:
    run = await run_workflow(temporal_client, Run())
    # The Localizer wrote version 1 and the dubbing stage version 2: the track, the
    # captions and the reviewer all see version 2.
    assert run.reviewing.layouts[0]["script_key"] == run.result.dub.script_key
    assert run.reviewing.layouts[0]["script_version"] == 2
    assert run.reviewing.reviews[0]["script_key"] == run.result.dub.script_key


# --- a review that fails: which stage runs again ----------------------------------------


async def test_a_line_read_badly_runs_only_the_dubber_for_that_line_and_what_follows(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[verdict(issue("dub", "read too fast", segment_id=1)), verdict()]
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    # Nothing before the dubbing was done again.
    assert run.prep.calls == {"ingest": 1, "transcribe": 1, "detect_subtitles": 1}
    assert run.localizing.calls["assemble_script"] == 1
    assert run.localizing.model.answer_attempts == 1
    # The Dubber spoke only the line the review named, and nothing was spoken before it
    # (no line was too long the first time).
    assert run.dubbing.ports.spoken
    assert all(text.startswith("câu ngắn 1.") for text in run.dubbing.ports.spoken)
    assert run.dubbing.rounds_seen == [0, 1]
    assert [r for r, _ in run.dubbing.committed] == [0, 1]
    # The new track, captions and render are from the new script, in round 1.
    assert [x["round"] for x in run.reviewing.layouts] == [0, 1]
    second = run.reviewing.layouts[1]
    assert second["script_key"] == "work/7001/r1/script.v3.json"
    assert second["audio_key"] == "work/7001/r1/dubbed.wav"
    assert second["segments_key"] == "work/7001/r1/dub-segments.json"
    assert run.reviewing.reviews[1]["script_key"] == second["script_key"]
    assert run.reviewing.calls["render"] == 2
    assert run.reviewing.calls["review_render"] == 2
    # What is published is the second render.
    assert run.reviewing.store.get_bytes(OUTPUT, produced_by="test") == b"mp4:" + (
        second["sha"].encode()
    )


async def test_the_lines_the_dubber_decided_before_keep_their_decisions(
    temporal_client: Client,
) -> None:
    from tests.support.dub_fakes import ScriptedDubber

    run = Run()
    run.dubbing = DubFakes(unfit_ids=(0,), dubber=ScriptedDubber(rewrites_per_segment=1))
    run.reviewing = ReviewFakes(
        verdicts=[verdict(issue("dub", "read too fast", segment_id=1)), verdict()]
    )
    await run_workflow(temporal_client, run)
    first, second = run.dubbing.committed
    assert [d.id for d in first[1].items] == [0]
    # The second commit knows the decision about line 0 as well as the new one for line 1.
    assert [d.id for d in second[1].items] == [0, 1]
    # ... but only the new decision was applied to the script.
    assert [d.id for d in run.dubbing.decisions_seen[1].items] == [1]


async def test_a_wrong_translation_runs_the_localizer_again_with_the_notes_and_what_follows(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[
            verdict(
                issue("localize", "too formal for this channel", segment_id=0),
                issue("dub", "also rushed", segment_id=1),
            ),
            verdict(),
        ]
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    # Transcript and subtitles are not made again; the Localizer is, once more, as the
    # next version in round 1.
    assert run.prep.calls == {"ingest": 1, "transcribe": 1, "detect_subtitles": 1}
    assert run.localizing.assembled == [(0, 1), (1, 3)]
    assert run.localizing.model.answer_attempts == 2
    prompt = str(run.localizing.model.seen_messages[-1])
    assert "too formal for this channel" in prompt and "also rushed" in prompt
    assert "<untrusted_video_text>" in prompt
    # The dubbing runs for the new script, into round 1.
    assert run.dubbing.rounds_seen == [0, 1]
    second = run.reviewing.layouts[1]
    assert second["script_key"] == "work/7001/r1/script.v4.json"


async def test_a_layout_problem_runs_only_the_layout_and_the_render_again(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[verdict(issue("captions", "cut off")), verdict()], changed_layout_in_rounds={1}
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    assert run.prep.calls == {"ingest": 1, "transcribe": 1, "detect_subtitles": 1}
    assert run.localizing.calls["assemble_script"] == 1
    assert run.dubbing.calls["write_dubbed_script"] == 1
    assert run.dubbing.calls["assemble_audio"] == 1
    assert [x["round"] for x in run.reviewing.layouts] == [0, 1]
    assert run.reviewing.calls["render"] == 2


async def test_a_layout_that_comes_out_the_same_is_not_reviewed_twice(
    temporal_client: Client,
) -> None:
    """The layout is deterministic: run again on the same script and track it gives the
    same spec, so the same video, and a second review would only repeat the first."""
    run = Run()
    run.reviewing = ReviewFakes(verdicts=[verdict(issue("captions", "cut off")), verdict()])
    await run_workflow(temporal_client, run)
    assert run.reviewing.calls["review_render"] == 1
    assert run.reviewing.calls["render"] == 1
    assert run.result.publish.outcome == "needs_human"
    assert "same video" in " ".join(row(run)["record"]["reasons"])
    assert run.has(REVIEW) and not run.has(OUTPUT)


async def test_a_spec_that_is_rendered_again_does_not_render_again(
    temporal_client: Client,
) -> None:
    """Round one's layout for a caption problem that did not change the spec: asked to
    render the same spec twice, the renderer renders once (counted per spec hash)."""
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[verdict(issue("captions", "cut off")), verdict()], changed_layout_in_rounds={1}
    )
    await run_workflow(temporal_client, run)
    assert set(run.reviewing.rendered.values()) == {1}
    assert len(run.reviewing.rendered) == 2


# --- a run that goes to a person ---------------------------------------------------------


async def test_a_run_that_fails_every_review_is_kept_for_a_person_and_not_published(
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
    assert run.result.publish.review_key == REVIEW
    assert run.has(REVIEW)
    with pytest.raises(ClientError) as info:
        run.reviewing.s3.head_object(Bucket="reup-media", Key=OUTPUT)
    assert info.value.response["Error"]["Code"] == "NoSuchKey"
    assert run.reviewing.calls["review_render"] == 2  # never a third
    assert row(run)["status"] == "needs_human" and row(run)["needs_human"] is True
    assert row(run)["review_rounds"] == 2
    assert row(run)["review"] == REVIEW and row(run)["output"] is None
    assert "did not pass after 2 round(s)" in " ".join(row(run)["record"]["reasons"])
    # The video that is kept is the second render, the last one the reviewer saw.
    assert run.reviewing.store.get_bytes(REVIEW, produced_by="test") == b"mp4:" + (
        run.reviewing.layouts[1]["sha"].encode()
    )


@pytest.mark.parametrize(
    "issues",
    [
        [issue("render", "black frames")],
        [issue("transcribe", "the speech was misheard")],
        [issue("dub", "read badly")],  # no segment named: nothing to run again
    ],
    ids=["render", "an earlier stage", "an unnamed line"],
)
async def test_a_problem_a_rerun_cannot_fix_goes_to_a_person_after_one_review(
    temporal_client: Client, issues: list
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(verdicts=[verdict(*issues), verdict()])
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "needs_human"
    assert run.reviewing.calls["review_render"] == 1
    assert run.dubbing.calls["write_dubbed_script"] == 1
    assert run.has(REVIEW) and not run.has(OUTPUT)


async def test_a_flagged_translation_is_never_published_even_when_the_review_passes(
    temporal_client: Client,
) -> None:
    run = Run()
    run.localizing = LocalizerFakes(flagged_ids=(0,))
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "needs_human"
    assert run.has(REVIEW) and not run.has(OUTPUT)
    assert "Localizer flagged segments [0]" in " ".join(row(run)["record"]["reasons"])
    assert row(run)["record"]["verdict"]["pass"] is True


async def test_a_flagged_run_is_not_sent_round_again_it_goes_to_the_person_with_the_verdict(
    temporal_client: Client,
) -> None:
    run = Run()
    run.localizing = LocalizerFakes(flagged_ids=(0,))
    run.reviewing = ReviewFakes(verdicts=[verdict(issue("dub", "rushed", segment_id=1))])
    await run_workflow(temporal_client, run)
    assert run.reviewing.calls["review_render"] == 1
    assert run.dubbing.calls["write_dubbed_script"] == 1
    assert run.result.publish.outcome == "needs_human"


async def test_a_refused_dubber_rewrite_is_never_published(temporal_client: Client) -> None:
    run = Run()
    run.dubbing = DubFakes(refused_ids=(1,))
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "needs_human"
    assert run.has(REVIEW) and not run.has(OUTPUT)
    assert "segments [1] was refused" in " ".join(row(run)["record"]["reasons"])


async def test_the_flag_of_a_translation_made_again_is_the_new_ones(
    temporal_client: Client,
) -> None:
    """A flagged first translation is replaced after a review: the flag that matters is
    the one of the translation that is published."""
    run = Run()
    run.localizing = LocalizerFakes(flagged_ids=())
    run.reviewing = ReviewFakes(
        verdicts=[verdict(issue("localize", "wrong", segment_id=0)), verdict()]
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"


# --- a stage that fails for good ---------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "kind"),
    [
        ("layout_captions", "UncoveredGlyphs"),
        ("layout_captions", "ImplausibleCover"),
        ("layout_captions", "VideoOutOfBounds"),
        ("render", "RenderDeadline"),
        ("render", "FontMismatch"),
    ],
)
async def test_a_permanent_failure_ends_the_run_with_its_reason_on_it_and_nothing_published(
    temporal_client: Client, step: str, kind: str
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        fail_on={(step, 1): ApplicationError(f"{kind} happened", type=kind, non_retryable=True)}
    )
    await run_workflow(temporal_client, run)
    assert isinstance(run.outcome, BaseException)
    # Not retried: the step was asked once.
    assert run.reviewing.calls[step] == 1
    assert run.reviewing.calls["review_render"] == 0
    assert not run.has(OUTPUT) and not run.has(REVIEW)
    assert row(run)["status"] == "failed" and row(run)["needs_human"] is False
    assert row(run)["output"] is None and row(run)["review"] is None
    (reason,) = row(run)["record"]["reasons"]
    assert reason.startswith(f"{kind}: ") and f"{kind} happened" in reason


async def test_a_failure_that_cannot_be_written_on_the_run_does_not_hide_the_failure(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        fail_on={
            ("layout_captions", 1): ApplicationError(
                "bad glyph", type="UncoveredGlyphs", non_retryable=True
            )
        }
    )

    async def broken(**_: object) -> None:
        raise RuntimeError("database is down")

    run.reviewing.runs.upsert = broken  # type: ignore[method-assign]
    await run_workflow(temporal_client, run)
    assert isinstance(run.outcome, BaseException)
    # The workflow failed with the layout's own error, not with the database's.
    assert run.outcome.__cause__.__cause__.type == "UncoveredGlyphs"


async def test_a_flaky_render_is_retried_and_the_run_still_publishes(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(fail_on={("render", 1): RuntimeError("chromium crashed")})
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    assert run.reviewing.calls["render"] == 2


# --- the activities' contract with the workflow -----------------------------------------


async def test_an_activity_with_a_default_still_gets_every_argument_converted(
    temporal_client: Client,
) -> None:
    """Called with fewer arguments than it has parameters, an activity gets none of
    them as their types. The workflow always sends the full list, so this one, written
    with defaults like the real ones, receives models."""
    seen: list[type] = []

    @activity.defn(name="assemble_script")
    async def assemble_script(spec, transcript_key, subtitle_key, lines, round=0, version=1):  # type: ignore[no-untyped-def]
        seen.append(type(spec))
        raise ApplicationError("stop", type="PermanentProviderError", non_retryable=True)

    run = Run()
    run.localizing = LocalizerFakes(assemble=assemble_script)
    await run_workflow(temporal_client, run, timeout=20)
    # Untyped parameters are not converted: that is the failure mode this guards, so the
    # untyped stand-in sees a dict. The typed fakes everywhere else are the proof for
    # the real shapes; this one pins that the SDK behaves this way.
    assert seen and seen[0] is dict


async def test_an_agent_that_gives_up_during_a_rerun_is_written_on_the_run_too(
    temporal_client: Client,
) -> None:
    """The Localizer is sent round again and cannot satisfy its own validator: the agent
    raises in the workflow, not in an activity, and the run must still say why."""
    from tests.support.localizer_fakes import ScriptedModel, translate_all

    run = Run()
    run.localizing = LocalizerFakes(
        model=ScriptedModel(answers=(translate_all, lambda ids: translate_all(ids[:-1])))
    )
    run.reviewing = ReviewFakes(verdicts=[verdict(issue("localize", "wrong", segment_id=0))])
    await run_workflow(temporal_client, run)
    assert isinstance(run.outcome, BaseException)
    assert row(run)["status"] == "failed"
    (reason,) = row(run)["record"]["reasons"]
    assert reason.startswith("UnexpectedModelBehavior: ")
    assert not run.has(OUTPUT) and not run.has(REVIEW)


# --- a review that cannot be made, and failures before the layout -----------------------


async def test_a_review_that_cannot_be_made_keeps_the_render_for_a_person(
    temporal_client: Client,
) -> None:
    """The video exists and nobody could judge it (model, provider or OCR failed): it goes
    to `review/` with the reason, never to `output/` and never thrown away."""
    run = Run()
    run.reviewing = ReviewFakes(
        fail_on={
            ("review_render", 1): ApplicationError(
                "the verdict never validated", type="UnexpectedModelBehavior", non_retryable=True
            )
        }
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "needs_human"
    assert run.has(REVIEW) and not run.has(OUTPUT)
    assert row(run)["status"] == "needs_human" and row(run)["needs_human"] is True
    assert row(run)["record"]["verdict"] is None
    (reason,) = row(run)["record"]["reasons"]
    assert reason.startswith("the review could not be made: UnexpectedModelBehavior: ")
    assert run.reviewing.store.get_bytes(REVIEW, produced_by="test").startswith(b"mp4:")


async def test_a_review_that_fails_on_both_attempts_is_kept_for_a_person_too(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        fail_on={
            ("review_render", 1): RuntimeError("429 from the provider"),
            ("review_render", 2): RuntimeError("429 from the provider"),
        }
    )
    await run_workflow(temporal_client, run)
    assert run.reviewing.calls["review_render"] == 2
    assert run.result.publish.outcome == "needs_human"
    assert run.has(REVIEW) and not run.has(OUTPUT)


async def test_a_review_that_cannot_be_made_in_the_second_round_keeps_the_second_render(
    temporal_client: Client,
) -> None:
    run = Run()
    run.reviewing = ReviewFakes(
        verdicts=[verdict(issue("dub", "rushed", segment_id=1))],
        fail_on={
            ("review_render", 2): ApplicationError(
                "no usable answer", type="UnexpectedModelBehavior", non_retryable=True
            )
        },
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "needs_human"
    assert run.reviewing.store.get_bytes(REVIEW, produced_by="test") == b"mp4:" + (
        run.reviewing.layouts[1]["sha"].encode()
    )
    reasons = " ".join(row(run)["record"]["reasons"])
    assert "did not pass after 1 round(s)" in reasons and "could not be made" in reasons


async def test_a_localizer_that_gives_up_on_the_first_pass_is_written_on_the_run(
    temporal_client: Client,
) -> None:
    from tests.support.localizer_fakes import ScriptedModel, translate_all

    run = Run()
    run.localizing = LocalizerFakes(
        model=ScriptedModel(answers=(lambda ids: translate_all(ids[:-1]),))
    )
    await run_workflow(temporal_client, run)
    assert isinstance(run.outcome, BaseException)
    assert row(run)["status"] == "failed" and row(run)["needs_human"] is False
    (reason,) = row(run)["record"]["reasons"]
    assert reason.startswith("UnexpectedModelBehavior: ")
    assert run.reviewing.calls["layout_captions"] == 0
    assert not run.has(OUTPUT) and not run.has(REVIEW)


async def test_a_permanent_speech_error_is_written_on_the_run(temporal_client: Client) -> None:
    run = Run()
    run.dubbing = DubFakes(
        fail_on={
            ("synthesize_segments", 1): ApplicationError(
                "the voice does not exist", type="PermanentProviderError", non_retryable=True
            )
        }
    )
    await run_workflow(temporal_client, run)
    assert isinstance(run.outcome, BaseException)
    assert row(run)["status"] == "failed"
    (reason,) = row(run)["record"]["reasons"]
    assert reason.startswith("PermanentProviderError: ")
    assert run.reviewing.calls["layout_captions"] == 0


async def test_a_failure_before_the_localizer_is_written_on_the_run_too(
    temporal_client: Client,
) -> None:
    run = Run()
    run.prep = PrepFakes(lose_vocals_on_ingest_runs={1, 2})
    await run_workflow(temporal_client, run)
    assert isinstance(run.outcome, BaseException)
    assert row(run)["status"] == "failed"
