"""The dubbing stage inside the real workflow: the order of its steps, which queue
each runs on, when the Dubber is used, what the track is built from, and what happens
when an artifact it reads is lost. Activities are stand-ins with the real shapes; the
Dubber is the real agent on a scripted model."""

import asyncio
import uuid
from collections import Counter

from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from localization.contracts import (
    DubDecision,
    DubDecisions,
    LocalizationInput,
    LocalizationResult,
    LocalizedLines,
    LocalizedScriptRef,
    ScriptResult,
)
from localization.storage.object_store import work_key
from tests.support.dub_fakes import DubFakes, FakeDubberPorts, ScriptedDubber
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.workers.test_queue_concurrency import max_overlap


async def run_workflow(client: Client, dubbing: DubFakes, *, tts_limit=None, timeout=45):
    async with prep_workers(
        client, PrepFakes(), LocalizerFakes(), dubbing=dubbing, tts_limit=tts_limit
    ):
        handle = await client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        try:
            return handle, await asyncio.wait_for(handle.result(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return handle, exc


async def scheduled(handle) -> list[tuple[str, str]]:
    history = await handle.fetch_history()
    return [
        (
            e.activity_task_scheduled_event_attributes.activity_type.name,
            e.activity_task_scheduled_event_attributes.task_queue.name,
        )
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


def names(events: list[tuple[str, str]]) -> list[str]:
    return [n for n, _ in events]


# --- the steps and their queues --------------------------------------------------------


async def test_the_stage_runs_its_steps_in_order_each_on_its_own_queue(
    temporal_client: Client,
) -> None:
    handle, result = await run_workflow(temporal_client, DubFakes(hashes=3))
    assert isinstance(result, LocalizationResult), result
    events = await scheduled(handle)
    after_script = names(events)[names(events).index("assemble_script") + 1 :]
    # What follows the track (layout, render, review, publish) is the review loop's.
    assert after_script[: after_script.index("assemble_audio") + 1] == [
        "voice_profile",
        # Three lines are two batches, one per concurrent `tts` slot.
        "plan_synthesis",
        "synthesize_segments",
        "synthesize_segments",
        "measure_fit",
        "write_dubbed_script",
        "plan_synthesis",
        "synthesize_segments",
        "synthesize_segments",
        "commit_dub",
        "assemble_audio",
    ]
    queue = dict(events)
    assert queue["voice_profile"] == "io"
    assert queue["plan_synthesis"] == "io"
    assert queue["synthesize_segments"] == "tts"
    assert queue["measure_fit"] == "io"
    assert queue["write_dubbed_script"] == "io"
    assert queue["commit_dub"] == "io"
    assert queue["assemble_audio"] == "media"


async def test_the_result_names_the_track_and_the_script_it_was_built_from(
    temporal_client: Client,
) -> None:
    _, result = await run_workflow(temporal_client, DubFakes())
    assert isinstance(result, LocalizationResult), result
    assert result.dub is not None
    assert result.dub.audio_key == "work/7001/r0/dubbed.wav"
    assert result.dub.script_version == 2
    assert result.dub.script_key == "work/7001/r0/script.v2.json"
    assert result.dub.segments_key == "work/7001/r0/dub-segments.json"
    # The Localizer's own script is still the one the result reports for that stage.
    assert result.script is not None and result.script.script.version == 1


async def test_synthesis_is_sent_in_even_batches_of_at_most_ten_hashes_that_cover_the_plan(
    temporal_client: Client,
) -> None:
    dubbing = DubFakes(hashes=25)
    _, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    planned = [f"{i:064x}" for i in range(25)]
    # The first pass, then the second pass over the dubbed script (cache hits in real
    # life): three batches each, 9 + 8 + 8.
    for first, second in ((0, 3), (3, 6)):
        pass_batches = dubbing.batches[first:second]
        assert sorted(len(b) for b in pass_batches) == [8, 8, 9]
        assert sorted(h for b in pass_batches for h in b) == sorted(planned)
    assert len(dubbing.batches) == 6


async def test_nothing_is_synthesized_for_a_script_with_no_lines(temporal_client: Client) -> None:
    dubbing = DubFakes(hashes=0)
    handle, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    assert dubbing.calls["synthesize_segments"] == 0
    assert result.dub is not None


async def test_the_engine_is_read_once_and_passed_to_every_later_step(
    temporal_client: Client,
) -> None:
    dubbing = DubFakes(engine="engine-recorded")
    _, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    assert dubbing.calls["voice_profile"] == 1


# --- the Dubber ------------------------------------------------------------------------


async def test_the_dubber_is_not_run_when_every_line_fits(temporal_client: Client) -> None:
    dubbing = DubFakes(unfit_ids=())
    handle, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    assert not any(n.startswith("agent__dubber") for n in names(await scheduled(handle)))
    assert dubbing.decisions_seen == [DubDecisions(items=())]


async def test_the_dubber_runs_for_the_lines_that_do_not_fit_and_its_decisions_are_applied(
    temporal_client: Client,
) -> None:
    dubbing = DubFakes(unfit_ids=(0, 1), dubber=ScriptedDubber(rewrites_per_segment=1, stretch=1.1))
    handle, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    events = await scheduled(handle)
    counts = Counter(events)
    # One model turn that speaks both lines, and the answer.
    assert counts[("agent__dubber__model_request", "llm")] == 2
    # Speaking is a tool on the tts queue: one call per line.
    assert counts[("agent__dubber__toolset__<agent>__call_tool", "tts")] == 2
    assert sorted(dubbing.ports.spoken) == ["câu ngắn 0.0", "câu ngắn 1.0"]
    assert dubbing.decisions_seen == [
        DubDecisions(
            items=(
                DubDecision(id=0, text_vi="câu ngắn 0.0", stretch_ratio=1.1),
                DubDecision(id=1, text_vi="câu ngắn 1.0", stretch_ratio=1.1),
            )
        )
    ]
    # The new script is written after the Dubber, and the clips are made again after it.
    order = names(events)
    assert order.index("measure_fit") < order.index("agent__dubber__model_request")
    assert order.index("agent__dubber__model_request") < order.index("write_dubbed_script")
    assert order.index("write_dubbed_script") < order.index("commit_dub")


async def test_the_dubber_is_told_which_lines_to_shorten(temporal_client: Client) -> None:
    dubbing = DubFakes(unfit_ids=(1,))
    dubbing.dubber = ScriptedDubber(rewrites_per_segment=0)
    handle, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    assert [i.id for i in dubbing.decisions_seen[0].items] == [1]


async def test_a_dubber_that_never_decides_every_line_fails_the_run_instead_of_dubbing(
    temporal_client: Client,
) -> None:
    class Forgetful(ScriptedDubber):
        def model(self):
            inner = super().model()
            base = inner.function

            async def respond(messages, info):
                response = await base(messages, info)
                for part in response.parts:
                    if part.tool_name == info.output_tools[0].name:
                        part.args = {"items": part.args["items"][:1]}
                return response

            inner.function = respond
            return inner

    dubbing = DubFakes(unfit_ids=(0, 1), dubber=Forgetful(rewrites_per_segment=0))
    handle, result = await run_workflow(temporal_client, dubbing, timeout=60)
    assert isinstance(result, Exception)
    assert dubbing.calls["write_dubbed_script"] == 0
    assert dubbing.calls["assemble_audio"] == 0


# --- the tts queue limits the Dubber too ---------------------------------------------------


async def _dubber_overlap(client: Client, limit: int, lines: int) -> int:
    ports = FakeDubberPorts(speak_seconds=0.4)
    dubbing = DubFakes(
        unfit_ids=tuple(range(lines)),
        ports=ports,
        dubber=ScriptedDubber(rewrites_per_segment=1),
    )
    _, result = await run_workflow(client, dubbing, tts_limit=limit, timeout=60)
    assert isinstance(result, LocalizationResult), result
    assert len(ports.spans) == lines
    return max_overlap(ports.spans)


async def test_the_dubbers_tts_calls_never_exceed_the_tts_queue_limit(
    temporal_client: Client,
) -> None:
    assert await _dubber_overlap(temporal_client, limit=2, lines=6) == 2


async def test_control_a_higher_tts_limit_lets_the_dubbers_calls_overlap_more(
    temporal_client: Client,
) -> None:
    """The same run with the limit at 4 must show 4 at once, or the test above could
    not tell a limit from calls that never ran side by side."""
    assert await _dubber_overlap(temporal_client, limit=4, lines=6) == 4


# --- artifacts that go missing ---------------------------------------------------------------


async def test_a_music_stem_that_is_gone_is_made_again_once_and_the_track_is_built(
    temporal_client: Client,
) -> None:
    dubbing = DubFakes(missing_on={("assemble_audio", 1): "ingest"})
    handle, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    assert result.dub is not None
    acts = Counter(names(await scheduled(handle)))
    assert acts["ingest"] == 2
    assert dubbing.calls["assemble_audio"] == 2


async def test_a_second_loss_of_the_same_stage_fails_the_run(temporal_client: Client) -> None:
    dubbing = DubFakes(
        missing_on={("assemble_audio", 1): "ingest", ("assemble_audio", 2): "ingest"}
    )
    handle, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, Exception)
    assert dubbing.calls["assemble_audio"] == 2
    assert names(await scheduled(handle)).count("ingest") == 2


async def test_a_script_that_is_gone_is_written_again_by_the_localizer_and_dubbing_restarts(
    temporal_client: Client,
) -> None:
    dubbing = DubFakes(missing_on={("measure_fit", 1): "localize"})
    handle, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    acts = Counter(names(await scheduled(handle)))
    assert acts["assemble_script"] == 2
    assert dubbing.calls["voice_profile"] == 2
    assert dubbing.calls["assemble_audio"] == 1


async def test_a_script_made_again_with_a_flag_is_the_script_the_result_reports(
    temporal_client: Client,
) -> None:
    """The Localizer is a model: run again after a loss, it may write a line the first
    run did not. The review flag in the result is the one of the script the track was
    built from, not of the first attempt."""
    runs: list[int] = []

    @activity.defn(name="assemble_script")
    async def assemble_script(
        spec: LocalizationInput,
        transcript_key: str,
        subtitle_key: str,
        lines: LocalizedLines,
        round: int = 0,
        version: int = 1,
    ) -> ScriptResult:
        runs.append(1)
        flagged = len(runs) >= 2
        return ScriptResult(
            script=LocalizedScriptRef(
                script_key=work_key(spec.aweme_id, 0, "script.v1.json"), version=1
            ),
            needs_human=flagged,
            flagged_segment_ids=(1,) if flagged else (),
        )

    dubbing = DubFakes(missing_on={("measure_fit", 1): "localize"})
    async with prep_workers(
        temporal_client,
        PrepFakes(),
        LocalizerFakes(assemble=assemble_script),
        dubbing=dubbing,
    ):
        handle = await temporal_client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        result = await asyncio.wait_for(handle.result(), timeout=45)
    assert len(runs) == 2
    assert result.script is not None
    assert result.script.needs_human is True
    assert result.script.flagged_segment_ids == (1,)
    assert result.dub is not None


async def test_a_second_script_loss_fails_the_run(temporal_client: Client) -> None:
    dubbing = DubFakes(missing_on={("measure_fit", 1): "localize", ("measure_fit", 2): "localize"})
    _, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, Exception)
    assert dubbing.calls["assemble_audio"] == 0


async def test_a_dubbed_script_that_is_gone_restarts_the_dubbing_step_only(
    temporal_client: Client,
) -> None:
    dubbing = DubFakes(missing_on={("commit_dub", 1): "dub_script"})
    handle, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    acts = Counter(names(await scheduled(handle)))
    assert acts["assemble_script"] == 1
    assert dubbing.calls["write_dubbed_script"] == 2


# --- failures ------------------------------------------------------------------------------------


async def test_a_batch_that_fails_cancels_the_batches_still_waiting_before_the_stage_restarts(
    temporal_client: Client,
) -> None:
    """The others are paid for by the hash. When the stage goes on (the script was
    lost and is made again), the batches of the failed attempt must not keep running."""
    # Four batches start at once; the last one in the list is the one that fails, at
    # once, while the three before it are still speaking.
    dubbing = DubFakes(hashes=40, missing_hash={f"{39:064x}": "localize"}, batch_seconds=0.5)
    handle, result = await run_workflow(temporal_client, dubbing, tts_limit=4, timeout=60)
    assert isinstance(result, LocalizationResult), result
    history = await handle.fetch_history()
    cancelled = [
        e
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED
    ]
    assert len(cancelled) >= 3


async def test_a_batch_that_fails_for_good_stops_the_stage_before_the_track(
    temporal_client: Client,
) -> None:
    boom = ApplicationError("provider refused", type="PermanentProviderError", non_retryable=True)
    dubbing = DubFakes(hashes=25, fail_on={("synthesize_segments", 2): boom}, batch_seconds=0.2)
    _, result = await run_workflow(temporal_client, dubbing)
    assert isinstance(result, Exception)
    assert dubbing.calls["measure_fit"] == 0
    assert dubbing.calls["assemble_audio"] == 0
