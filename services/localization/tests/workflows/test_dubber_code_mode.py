"""The Dubber that writes code, inside the real workflow on a local Temporal server: each
call its code makes is an activity on its tool's queue, gathered calls really overlap, a
line is spoken at most three times however the calls are made, and what goes wrong in the
code ends where it ends for the tool loop. The model is scripted; what a real model writes
is not measured here."""

import json
from collections import Counter

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer

from localization.agents.dubber import MAX_REWRITES
from localization.contracts import LocalizationResult
from localization.contracts.models import STRETCH_MAX
from localization.storage.object_store import ArtifactMissingError
from localization.temporal.client import guarded_data_converter
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.support.dub_fakes import CodeDubber, DubFakes, FakeDubberPorts, speak_every_line
from tests.workers.test_queue_concurrency import max_overlap
from tests.workflows.test_dubbing_stage import names, run_workflow, scheduled

CALL_TOOL = "agent__dubber_code__toolset__<agent>__call_tool"


def _code(text: str):
    return lambda ids: text


def _cause(result: object) -> ApplicationError:
    """The error a failed run ended with."""
    assert isinstance(result, WorkflowFailureError), result
    cause = result.cause
    assert isinstance(cause, ApplicationError), cause
    return cause


async def _replays(handle) -> None:
    """A worker that restarts rebuilds the run from its history; the replay must take
    every decision the run took, or Temporal rejects it."""
    replayer = Replayer(
        workflows=[VideoLocalizationWorkflow],
        data_converter=guarded_data_converter(),
        plugins=[PydanticAIPlugin()],
    )
    await replayer.replay_workflow(await handle.fetch_history())


# --- the calls and their queues -------------------------------------------------------


async def test_each_gathered_synthesize_is_its_own_activity_on_the_tts_queue_and_they_overlap(
    temporal_client: Client,
) -> None:
    ports = FakeDubberPorts(speak_seconds=0.4)
    model = CodeDubber()
    dubbing = DubFakes(unfit_ids=(0, 1, 2), mode="code", ports=ports, dubber=model)
    handle, result = await run_workflow(temporal_client, dubbing, tts_limit=4, timeout=60)
    assert isinstance(result, LocalizationResult), result
    events = await scheduled(handle)
    # The mode is asked once, and the code mode's own agent runs: no tool-loop request.
    assert names(events).count("dubber_setup") == 1
    assert "dubber_context" not in names(events)
    assert not any(n.startswith("agent__dubber__") for n in names(events))
    tool_calls = Counter(queue for name, queue in events if name == CALL_TOOL)
    # Three reads on `io`, three speeches on `tts`; `accept` is pure and runs inline.
    assert tool_calls == {"io": 3, "tts": 3}
    assert sorted(ports.spoken) == ["câu ngắn 0", "câu ngắn 1", "câu ngắn 2"]
    # Measured, not assumed: the three speeches ran at the same time.
    assert max_overlap(ports.spans) == 3
    # Two model requests: the code, then the answer.
    assert names(events).count("agent__dubber_code__model_request") == 2
    assert [d.id for d in dubbing.decisions_seen[0].items] == [0, 1, 2]


async def test_control_a_tts_limit_of_one_runs_the_gathered_speeches_one_at_a_time(
    temporal_client: Client,
) -> None:
    """The overlap above must come from the gather, not from the measurement: the same
    code with one `tts` slot must show one at a time."""
    ports = FakeDubberPorts(speak_seconds=0.3)
    dubbing = DubFakes(unfit_ids=(0, 1, 2), mode="code", ports=ports, dubber=CodeDubber())
    _, result = await run_workflow(temporal_client, dubbing, tts_limit=1, timeout=60)
    assert isinstance(result, LocalizationResult), result
    assert len(ports.spans) == 3
    assert max_overlap(ports.spans) == 1


# --- how often a line may be spoken ---------------------------------------------------

FIVE_TRIES = """\
results = []
for n in range(5):
    try:
        results.append(await synthesize(id=0, text_vi="lần " + str(n)))
    except Exception as e:
        results.append("refused: " + str(e))
results
"""


async def test_code_cannot_speak_a_line_more_than_three_times_and_is_told_why(
    temporal_client: Client,
) -> None:
    ports = FakeDubberPorts()
    model = CodeDubber(snippets=(_code(FIVE_TRIES),))
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=ports, dubber=model)
    handle, result = await run_workflow(temporal_client, dubbing, timeout=60)
    assert isinstance(result, LocalizationResult), result
    assert MAX_REWRITES == 3
    assert ports.spoken == ["lần 0", "lần 1", "lần 2"]
    returned = json.loads(model.seen[0].replace("'", '"'))
    refusals = [r for r in returned if r.startswith("refused")]
    assert len(refusals) == 2
    assert all(f"accept it at {STRETCH_MAX}" in r for r in refusals)
    # A refused call never became an activity.
    tts = [n for n, q in await scheduled(handle) if n == CALL_TOOL and q == "tts"]
    assert len(tts) == MAX_REWRITES
    await _replays(handle)


GATHER_FIVE = """\
import asyncio
async def speak(n):
    try:
        return await synthesize(id=0, text_vi="lần " + str(n))
    except Exception as e:
        return "refused: " + str(e)
await asyncio.gather(*[speak(n) for n in range(5)])
"""


async def test_calls_gathered_for_one_line_cannot_all_pass_the_count_before_any_is_counted(
    temporal_client: Client,
) -> None:
    ports = FakeDubberPorts(speak_seconds=0.2)
    model = CodeDubber(snippets=(_code(GATHER_FIVE),))
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=ports, dubber=model)
    handle, result = await run_workflow(temporal_client, dubbing, timeout=60)
    assert isinstance(result, LocalizationResult), result
    assert len(ports.spoken) == MAX_REWRITES
    assert model.seen[0].count("has used its 3 rewrites") == 2
    await _replays(handle)


async def test_the_count_carries_over_from_one_snippet_to_the_next(
    temporal_client: Client,
) -> None:
    two = 'await synthesize(id=0, text_vi="a")\nawait synthesize(id=0, text_vi="b")'
    model = CodeDubber(snippets=(_code(two), _code(two)))
    ports = FakeDubberPorts()
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=ports, dubber=model)
    _, result = await run_workflow(temporal_client, dubbing, timeout=60)
    assert isinstance(result, LocalizationResult), result
    assert ports.spoken == ["a", "b", "a"]
    assert "has used its 3 rewrites" in model.seen[1]


async def test_each_line_has_its_own_three_rewrites(temporal_client: Client) -> None:
    code = """\
import asyncio
await asyncio.gather(*[synthesize(id=i, text_vi="x" + str(n)) for i in (0, 1) for n in range(3)])
"""
    ports = FakeDubberPorts()
    dubbing = DubFakes(
        unfit_ids=(0, 1), mode="code", ports=ports, dubber=CodeDubber(snippets=(_code(code),))
    )
    _, result = await run_workflow(temporal_client, dubbing, timeout=60)
    assert isinstance(result, LocalizationResult), result
    assert len(ports.spoken) == 6


THREE_THEN_ONE_MORE = """\
spoken = []
for n in range(3):
    spoken.append(await synthesize(id=0, text_vi="lần " + str(n)))
try:
    spoken.append(await synthesize(id=0, text_vi="lần 3"))
except Exception as e:
    spoken.append("refused: " + str(e))
spoken
"""


async def test_an_attempt_that_failed_and_was_retried_is_counted_once(
    temporal_client: Client,
) -> None:
    """The first speech's activity fails once and is retried by Temporal: the line has
    still been spoken once, so two more are allowed and the fourth is refused."""
    ports = FakeDubberPorts(speak_failures=1)
    model = CodeDubber(snippets=(_code(THREE_THEN_ONE_MORE),))
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=ports, dubber=model)
    handle, result = await run_workflow(temporal_client, dubbing, timeout=90)
    assert isinstance(result, LocalizationResult), result
    assert ports.calls["speak"] == 4  # one failed attempt and three that spoke
    assert ports.spoken == ["lần 0", "lần 1", "lần 2"]
    assert model.seen[0].count("has used its 3 rewrites") == 1
    # The retry is an attempt of the same activity, not a new call.
    tts = [n for n, q in await scheduled(handle) if n == CALL_TOOL and q == "tts"]
    assert len(tts) == MAX_REWRITES
    history = await handle.fetch_history()
    attempts = [
        e.activity_task_started_event_attributes.attempt
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
    ]
    assert 2 in attempts
    await _replays(handle)


# --- what goes wrong in the code ------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "told"),
    [
        # The sandbox checks a first snippet before running it, so this is where a
        # syntax error is caught.
        ("this is not python (", "invalid-syntax"),
        ('raise ValueError("no idea what to do")', "Runtime error"),
    ],
    ids=["does-not-parse", "raises-as-it-runs"],
)
async def test_code_that_keeps_failing_ends_the_run_as_the_dubber_giving_up(
    temporal_client: Client, code: str, told: str
) -> None:
    model = CodeDubber(snippets=(_code(code),) * 10)
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", dubber=model)
    handle, result = await run_workflow(temporal_client, dubbing, timeout=60)
    cause = _cause(result)
    assert "the Dubber gave up" in cause.message
    assert cause.type == "UnexpectedModelBehavior"
    assert cause.non_retryable
    # Each failure went back to the model, three times, and the fourth ended the run.
    assert len(model.seen) == 3
    assert all(told in seen for seen in model.seen)
    assert dubbing.calls["write_dubbed_script"] == 0
    await _replays(handle)


async def test_code_that_runs_past_the_tool_call_limit_ends_the_run_as_the_dubber_giving_up(
    temporal_client: Client,
) -> None:
    runaway = _code(
        'for n in range(500):\n    await estimate_duration(texts=["một hai ba"])\n"done"'
    )
    model = CodeDubber(snippets=(runaway,))
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", dubber=model)
    handle, result = await run_workflow(temporal_client, dubbing, timeout=60)
    cause = _cause(result)
    assert "the Dubber gave up" in cause.message
    assert cause.type == "UsageLimitExceeded"
    assert dubbing.calls["write_dubbed_script"] == 0
    await _replays(handle)


MID_GATHER = """\
import asyncio
await asyncio.gather(
    synthesize(id=0, text_vi="đầu 0"),
    synthesize(id=1, text_vi="đầu 1"),
    synthesize(id=99, text_vi="lạ"),
)
"""


async def test_a_refusal_raised_in_the_middle_of_a_gather_comes_back_to_the_model(
    temporal_client: Client,
) -> None:
    """The uncaught refusal ends that snippet with the error and the calls that had
    started; the model's next snippet finishes the work, and the speeches of the first
    count toward each line's three."""
    ports = FakeDubberPorts(speak_seconds=0.3)
    model = CodeDubber(snippets=(_code(MID_GATHER), speak_every_line))
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=ports, dubber=model)
    handle, result = await run_workflow(temporal_client, dubbing, timeout=60)
    assert isinstance(result, LocalizationResult), result
    first = model.seen[0]
    assert "segment 99 is not one of the lines to shorten" in first
    assert "câu ngắn 0" in ports.spoken and "câu ngắn 1" in ports.spoken
    assert [d.id for d in dubbing.decisions_seen[0].items] == [0, 1]
    await _replays(handle)


CATCH_EVERYTHING = """\
try:
    await get_segment(id=0)
except Exception as e:
    print("ignored", e)
"done"
"""


async def test_an_activity_that_fails_for_good_ends_the_run_even_when_the_code_catches_it(
    temporal_client: Client,
) -> None:
    """The script is gone: as in the tool loop, the stage that wrote it runs again,
    whatever the code did with the error."""
    ports = FakeDubberPorts(
        segment_errors=[ArtifactMissingError("work/7001/r0/script.v1.json", "localize")]
    )
    model = CodeDubber(snippets=(_code(CATCH_EVERYTHING), speak_every_line))
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=ports, dubber=model)
    handle, result = await run_workflow(temporal_client, dubbing, timeout=60)
    assert isinstance(result, LocalizationResult), result
    acts = Counter(names(await scheduled(handle)))
    assert acts["assemble_script"] == 2
    assert acts["dubber_setup"] == 2
    await _replays(handle)
