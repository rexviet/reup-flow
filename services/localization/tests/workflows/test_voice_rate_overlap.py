"""The voice's speaking rate is measured while the video is ingested: the two do not
depend on each other, so the run does not wait for them one after the other.

Each test says which activities were in flight together, from the activities' own side
(a rendezvous only both can leave) and from the clock, not from the order the workflow
scheduled them in."""

import time

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from tests.support.localizer_fakes import LocalizerFakes, ScriptedModel
from tests.support.prep_fakes import PrepFakes
from tests.support.rendezvous import Rendezvous
from tests.workflows.test_review_loop import Run, row, run_workflow


def run_with(*, prep: PrepFakes, localizing: LocalizerFakes) -> Run:
    run = Run()
    run.prep = prep
    run.localizing = localizing
    return run


async def workflow_seconds(run: Run) -> float:
    """How long the workflow itself ran: the test's own clock also counts the workers
    shutting down."""
    described = await run.handle.describe()
    assert described.close_time is not None
    return (described.close_time - described.start_time).total_seconds()


async def cancel_requested(run: Run) -> list[str]:
    """The activities the workflow asked to stop, by name."""
    history = await run.handle.fetch_history()
    scheduled = {
        e.event_id: e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    }
    return [
        scheduled[e.activity_task_cancel_requested_event_attributes.scheduled_event_id]
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED
    ]


async def test_the_voice_rate_and_the_ingest_are_in_flight_at_the_same_time(
    temporal_client: Client,
) -> None:
    meeting = Rendezvous("ingest", "rate")
    run = run_with(
        prep=PrepFakes(meeting=meeting, ingest_seconds=1.0),
        localizing=LocalizerFakes(meeting=meeting, rate_seconds=1.0),
    )
    started = time.monotonic()
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published", run.outcome
    # Each side left the rendezvous only because the other was in flight too.
    assert meeting.met == {"ingest", "rate"}
    (i_start, i_end), (r_start, r_end) = run.prep.ingest_spans[0], run.localizing.rate_spans[0]
    assert max(i_start, r_start) < min(i_end, r_end), "they did not overlap"
    # ... so together they took about as long as the slower one, not as long as both.
    together = max(i_end, r_end) - min(i_start, r_start)
    assert together < (i_end - i_start) + (r_end - r_start) - 0.5
    assert time.monotonic() - started < 60


async def test_the_preparation_goes_on_while_the_rate_is_still_being_measured(
    temporal_client: Client,
) -> None:
    run = run_with(prep=PrepFakes(), localizing=LocalizerFakes(rate_seconds=2.0))
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    rate_end = run.localizing.rate_spans[0][1]
    starts = {name: start for name, start, _ in run.prep.spans}
    assert starts["transcribe"] < rate_end and starts["detect_subtitles"] < rate_end


async def test_the_rate_is_measured_once_and_the_localizer_works_from_it(
    temporal_client: Client,
) -> None:
    run = run_with(
        prep=PrepFakes(),
        localizing=LocalizerFakes(
            rate=5.0, model=ScriptedModel(tool_calls=(("read_transcript", {}),))
        ),
    )
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    assert run.localizing.calls["voice_rate"] == 1
    # The first line's slot at 5 syllables a second, as the model is told it.
    first = run.localizing.ports.segment_list[0]
    slot = first.slot_ms if first.slot_ms is not None else first.end_ms - first.start_ms
    assert f"up to {round(slot / 1000 * 5.0)} syllables" in str(run.localizing.model.seen_messages)


async def test_a_voice_whose_rate_is_known_already_costs_the_run_nothing(
    temporal_client: Client,
) -> None:
    """The cached-voice path: the activity answers at once while the ingest is still
    running, and the run goes on from there."""
    run = run_with(prep=PrepFakes(ingest_seconds=0.5), localizing=LocalizerFakes(rate_seconds=0.0))
    await run_workflow(temporal_client, run)
    assert run.result.publish.outcome == "published"
    assert run.localizing.calls["voice_rate"] == 1
    assert run.localizing.rate_spans[0][1] < run.prep.ingest_spans[0][1]


async def test_a_voice_that_cannot_be_measured_fails_the_run_and_stops_the_ingest(
    temporal_client: Client,
) -> None:
    run = run_with(
        prep=PrepFakes(ingest_seconds=6.0),
        localizing=LocalizerFakes(
            rate_error=ApplicationError(
                "the voice is not known to the provider",
                type="PermanentProviderError",
                non_retryable=True,
            )
        ),
    )
    await run_workflow(temporal_client, run, timeout=30)
    # The run failed at once and did not wait for the ingest to finish.
    assert await workflow_seconds(run) < 4
    assert isinstance(run.outcome, BaseException)
    assert "ingest" in await cancel_requested(run)
    # Nothing that needs the video ran, and nothing was translated.
    assert run.prep.calls["transcribe"] == 0 and run.prep.calls["detect_subtitles"] == 0
    assert run.localizing.calls["assemble_script"] == 0
    reasons = " ".join(row(run)["record"]["reasons"])
    assert row(run)["status"] == "failed"
    assert "PermanentProviderError" in reasons and "voice is not known" in reasons


async def test_an_ingest_that_fails_stops_the_voice_rate_that_is_still_running(
    temporal_client: Client,
) -> None:
    run = run_with(
        prep=PrepFakes(
            ingest_error=ApplicationError(
                "the video cannot be read", type="PermanentProviderError", non_retryable=True
            )
        ),
        localizing=LocalizerFakes(rate_seconds=6.0),
    )
    await run_workflow(temporal_client, run, timeout=30)
    assert await workflow_seconds(run) < 4
    assert isinstance(run.outcome, BaseException)
    assert "voice_rate" in await cancel_requested(run)
    assert run.prep.calls["transcribe"] == 0
    assert "the video cannot be read" in " ".join(row(run)["record"]["reasons"])


async def test_a_voice_rate_that_fails_after_the_ingest_stops_the_stages_still_running(
    temporal_client: Client,
) -> None:
    """The rate is a sibling of 2a and 2b as well: when it fails while they run, they
    are stopped like any other sibling."""
    run = run_with(
        # Ingest is done at 2 s, the rate fails at 3 s, 2a and 2b would end at 4 s.
        prep=PrepFakes(delay_seconds=2.0),
        localizing=LocalizerFakes(
            rate_seconds=3.0,
            rate_error=ApplicationError(
                "no such voice", type="PermanentProviderError", non_retryable=True
            ),
        ),
    )
    await run_workflow(temporal_client, run, timeout=30)
    assert isinstance(run.outcome, BaseException)
    stopped = await cancel_requested(run)
    assert "transcribe" in stopped and "detect_subtitles" in stopped
    assert run.localizing.calls["assemble_script"] == 0
