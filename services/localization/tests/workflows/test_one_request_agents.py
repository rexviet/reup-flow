"""The Localizer and the Dubber that answer in one request, inside the real workflow:
how many model requests each makes, what the repair request carries, what is kept when
the lines are still wrong, and that `tools` mode still runs the loop. The agents run for
real (prompt, durable model activity, checks); only the model and the I/O behind the
agents are stand-ins with the real shapes."""

import asyncio
import uuid

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

from localization.agents.localizer import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from localization.contracts import LocalizationInput, LocalizationResult
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.support.before_review_projects_flow import BeforeReviewProjectsWorkflow
from tests.support.dub_fakes import DubFakes
from tests.support.inputs import make_input
from tests.support.localizer_fakes import (
    FakePorts,
    LocalizerFakes,
    OneRequestModel,
    ScriptedModel,
    segment_ids_in,
)
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers

SHORT = "xin chào các bạn"  # 4 syllables: 1000 ms at the 4 a second the fakes speak at
LONG = "một hai ba bốn năm sáu bảy tám chín mười một hai"  # 12 syllables: 3000 ms
TOO_LONG_FOR_ONE = "một hai ba bốn năm sáu bảy tám chín"  # 9 syllables: 2250 ms


def fits(prompt: str) -> list[dict]:
    return [{"id": i, "text_vi": SHORT} for i in segment_ids_in(prompt)]


async def run(
    client: Client,
    localizing: LocalizerFakes,
    dubbing: DubFakes | None = None,
    *,
    spec=None,
    workflow_type=VideoLocalizationWorkflow,
):
    async with prep_workers(
        client, PrepFakes(), localizing, dubbing=dubbing, workflow=workflow_type
    ):
        handle = await client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input() if spec is None else spec,
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        try:
            return handle, await asyncio.wait_for(handle.result(), timeout=45)
        except Exception as exc:  # noqa: BLE001
            return handle, exc


async def scheduled(handle) -> list[str]:
    history = await handle.fetch_history()
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


def model_requests(names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in names:
        if name.endswith("__model_request") and name.startswith("agent__"):
            out[name] = out.get(name, 0) + 1
    return out


# --- the Localizer ---------------------------------------------------------------------


async def test_a_translation_that_fits_takes_one_model_request_and_no_tool(
    temporal_client: Client,
) -> None:
    model = OneRequestModel(answers=(fits,))
    localizing = LocalizerFakes(mode="single", model=model)
    handle, result = await run(temporal_client, localizing)
    assert isinstance(result, LocalizationResult), result
    assert model.requests == 1
    assert model.tools_offered == []
    assert model_requests(await scheduled(handle)) == {"agent__localizer_single__model_request": 1}
    assert [(x.id, x.text_vi) for x in localizing.lines_seen[0].lines] == [(0, SHORT), (1, SHORT)]
    # The transcript was read once, ahead of the request: not once per tool call.
    assert localizing.ports.calls["segments"] == 1
    assert localizing.ports.calls["lookup_terms"] == 1


async def test_the_prompt_holds_the_whole_transcript_with_its_budgets_and_glossary(
    temporal_client: Client,
) -> None:
    model = OneRequestModel(answers=(fits,))
    ports = FakePorts(spans=[(0, 1000)], glossary={("chan_1", "手机"): "điện thoại"})
    handle, result = await run(
        temporal_client, LocalizerFakes(mode="single", model=model, ports=ports)
    )
    assert isinstance(result, LocalizationResult), result
    # The answer leaves 手机's Vietnamese out, so a repair follows; this is the first prompt.
    prompt = model.prompts[0]
    assert "[0] 0-1500 ms, slot 1500 ms, up to 6 syllables: 大家好" in prompt
    assert "[1] 1800-3000 ms, slot 1200 ms, up to 5 syllables: 今天" in prompt
    assert "Segments that overlap one: 0" in prompt
    assert "手机 → điện thoại" in prompt
    assert prompt.count(UNTRUSTED_OPEN) == prompt.count(UNTRUSTED_CLOSE) == 3


async def test_a_line_over_its_slot_gets_one_repair_request_with_only_that_line(
    temporal_client: Client,
) -> None:
    model = OneRequestModel(
        answers=(
            lambda prompt: [{"id": 0, "text_vi": SHORT}, {"id": 1, "text_vi": LONG}],
            lambda prompt: [{"id": i, "text_vi": "tạm biệt"} for i in segment_ids_in(prompt)],
        )
    )
    localizing = LocalizerFakes(mode="single", model=model)
    handle, result = await run(temporal_client, localizing)
    assert isinstance(result, LocalizationResult), result
    assert model.requests == 2
    first, repair = model.prompts
    assert segment_ids_in(first) == [0, 1]
    assert segment_ids_in(repair) == [1]
    assert LONG in repair and "1800 ms over" in repair and "at most 5 syllables" in repair
    assert "bản dịch" not in repair and SHORT not in repair
    assert model_requests(await scheduled(handle)) == {
        "agent__localizer_single__model_request": 1,
        "agent__localizer_repair__model_request": 1,
    }
    # The repaired line replaces the long one; the line that was fine is untouched.
    assert [(x.id, x.text_vi) for x in localizing.lines_seen[0].lines] == [
        (0, SHORT),
        (1, "tạm biệt"),
    ]


async def test_a_line_that_drops_a_glossary_term_gets_one_repair_request_naming_the_term(
    temporal_client: Client,
) -> None:
    """Segment 1 says 手机; the first answer leaves its glossary Vietnamese out."""
    model = OneRequestModel(
        answers=(
            fits,
            lambda prompt: [{"id": i, "text_vi": "điện thoại mới"} for i in segment_ids_in(prompt)],
        )
    )
    ports = FakePorts(glossary={("chan_1", "手机"): "điện thoại"})
    localizing = LocalizerFakes(mode="single", model=model, ports=ports)
    handle, result = await run(temporal_client, localizing)
    assert isinstance(result, LocalizationResult), result
    assert model.requests == 2
    _, repair = model.prompts
    assert segment_ids_in(repair) == [1]
    assert "手机 → điện thoại" in repair and "leaves out" in repair
    assert [(x.id, x.text_vi) for x in localizing.lines_seen[0].lines] == [
        (0, SHORT),
        (1, "điện thoại mới"),
    ]
    history = await handle.fetch_history()
    # A new run records the marker, so it replays with the check on.
    assert any(
        b"localizer-glossary-terms" in e.SerializeToString()
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    )


async def test_a_line_still_missing_its_term_after_the_repair_is_kept_not_fatal(
    temporal_client: Client,
) -> None:
    model = OneRequestModel(answers=(fits,))
    ports = FakePorts(glossary={("chan_1", "手机"): "điện thoại"})
    localizing = LocalizerFakes(mode="single", model=model, ports=ports)
    _, result = await run(temporal_client, localizing)
    assert isinstance(result, LocalizationResult), result
    assert model.requests == 2
    assert [(x.id, x.text_vi) for x in localizing.lines_seen[0].lines] == [(0, SHORT), (1, SHORT)]


async def test_a_line_still_too_long_after_the_repair_is_kept_for_the_dubbing_stage(
    temporal_client: Client,
) -> None:
    """No third request: the run goes on with the line as the repair left it, and the
    dubbing stage shortens or speeds up what does not fit, as it does for a line the tool
    loop could not fit."""
    model = OneRequestModel(
        answers=(
            lambda prompt: [{"id": 0, "text_vi": SHORT}, {"id": 1, "text_vi": LONG}],
            lambda prompt: [{"id": 1, "text_vi": TOO_LONG_FOR_ONE}],
        )
    )
    localizing = LocalizerFakes(mode="single", model=model)
    handle, result = await run(temporal_client, localizing)
    assert isinstance(result, LocalizationResult), result
    assert model.requests == 2
    assert [(x.id, x.text_vi) for x in localizing.lines_seen[0].lines] == [
        (0, SHORT),
        (1, TOO_LONG_FOR_ONE),
    ]


async def test_a_line_that_is_still_unusable_after_the_repair_ends_the_run(
    temporal_client: Client,
) -> None:
    model = OneRequestModel(
        answers=(lambda prompt: [{"id": 0, "text_vi": SHORT}, {"id": 1, "text_vi": "你好"}],)
    )
    localizing = LocalizerFakes(mode="single", model=model)
    handle, result = await run(temporal_client, localizing)
    assert isinstance(result, Exception)
    assert "the Localizer gave up" in str(result.__cause__) or "gave up" in repr(result)
    assert model.requests == 2
    assert localizing.calls["assemble_script"] == 0


async def test_a_missing_line_is_asked_for_in_the_repair_request(temporal_client: Client) -> None:
    model = OneRequestModel(
        answers=(
            lambda prompt: [{"id": 0, "text_vi": SHORT}],
            lambda prompt: [{"id": 1, "text_vi": "tạm biệt"}],
        )
    )
    localizing = LocalizerFakes(mode="single", model=model)
    handle, result = await run(temporal_client, localizing)
    assert isinstance(result, LocalizationResult), result
    assert segment_ids_in(model.prompts[1]) == [1]
    assert "no line was returned" in model.prompts[1]
    assert [x.id for x in localizing.lines_seen[0].lines] == [0, 1]


async def test_tools_mode_runs_the_tool_loop_as_before(temporal_client: Client) -> None:
    model = ScriptedModel()
    localizing = LocalizerFakes(mode="tools", model=model)
    handle, result = await run(temporal_client, localizing)
    assert isinstance(result, LocalizationResult), result
    names = await scheduled(handle)
    # A read, then the answer: two requests on the loop's own agent, none on the new ones.
    assert model_requests(names) == {"agent__localizer__model_request": 2}
    assert "agent__localizer__toolset__<agent>__call_tool" in names
    assert model.requests == 2
    assert localizing.ports.calls["segments"] >= 1


# --- the Dubber --------------------------------------------------------------------------


def shorter(prompt: str) -> list[dict]:
    ids = [
        int(i)
        for i in __import__("re").search(r"segment ids \[([0-9, ]*)\]", prompt)[1].split(", ")
    ]
    return [{"id": i, "text_vi": "câu ngắn"} for i in ids]


async def test_the_dubber_makes_one_model_request_and_speaks_nothing(
    temporal_client: Client,
) -> None:
    model = OneRequestModel(answers=(shorter,))
    dubbing = DubFakes(unfit_ids=(0, 1), mode="single")
    dubbing.dubber = model  # type: ignore[assignment]
    handle, result = await run(temporal_client, LocalizerFakes(), dubbing)
    assert isinstance(result, LocalizationResult), result
    assert model.requests == 1
    assert model.tools_offered == []
    assert model_requests(await scheduled(handle)) == {
        "agent__localizer__model_request": 2,
        "agent__dubber_single__model_request": 1,
    }
    # No TTS call to measure a rewrite: the line is read from the cache, once each.
    assert dubbing.ports.calls["speak"] == 0
    assert dubbing.ports.calls["segment"] == 2
    (decisions,) = dubbing.decisions_seen
    assert [(d.id, d.text_vi) for d in decisions.items] == [(0, "câu ngắn"), (1, "câu ngắn")]
    assert all(1.0 <= d.stretch_ratio <= 1.15 for d in decisions.items)
    (prompt,) = model.prompts
    assert "slot 1000 ms, about 4 syllables; speaking takes 1800 ms" in prompt
    assert prompt.count(UNTRUSTED_OPEN) == prompt.count(UNTRUSTED_CLOSE) == 2


async def test_a_rewrite_over_its_slot_gets_one_repair_request_with_only_that_line(
    temporal_client: Client,
) -> None:
    model = OneRequestModel(
        answers=(
            lambda prompt: [
                {"id": 0, "text_vi": "câu ngắn"},
                {"id": 1, "text_vi": LONG},
            ],
            lambda prompt: [{"id": 1, "text_vi": "ngắn hơn"}],
        )
    )
    dubbing = DubFakes(unfit_ids=(0, 1), mode="single")
    dubbing.dubber = model  # type: ignore[assignment]
    handle, result = await run(temporal_client, LocalizerFakes(), dubbing)
    assert isinstance(result, LocalizationResult), result
    assert model.requests == 2
    assert "The segment ids are [1]." in model.prompts[1]
    assert segment_ids_in(model.prompts[1]) == [1]
    assert "3000 ms" in model.prompts[1] and "at most 4 syllables" in model.prompts[1]
    assert "source: 你好" in model.prompts[1]
    assert model_requests(await scheduled(handle))["agent__dubber_repair__model_request"] == 1
    (decisions,) = dubbing.decisions_seen
    assert {d.id: d.text_vi for d in decisions.items} == {0: "câu ngắn", 1: "ngắn hơn"}


async def test_a_rewrite_still_over_after_the_repair_is_accepted_at_the_fastest_stretch(
    temporal_client: Client,
) -> None:
    model = OneRequestModel(
        answers=(lambda prompt: [{"id": 0, "text_vi": LONG}, {"id": 1, "text_vi": LONG}],)
    )
    dubbing = DubFakes(unfit_ids=(0, 1), mode="single")
    dubbing.dubber = model  # type: ignore[assignment]
    handle, result = await run(temporal_client, LocalizerFakes(), dubbing)
    assert isinstance(result, LocalizationResult), result
    assert model.requests == 2
    (decisions,) = dubbing.decisions_seen
    assert [(d.text_vi, d.stretch_ratio) for d in decisions.items] == [(LONG, 1.15), (LONG, 1.15)]


async def test_dubber_tools_mode_runs_the_tool_loop_as_before(temporal_client: Client) -> None:
    dubbing = DubFakes(unfit_ids=(0, 1), mode="tools")
    handle, result = await run(temporal_client, LocalizerFakes(), dubbing)
    assert isinstance(result, LocalizationResult), result
    counts = model_requests(await scheduled(handle))
    assert counts["agent__dubber__model_request"] == 2
    assert "agent__dubber_single__model_request" not in counts
    assert dubbing.ports.calls["speak"] == 2


# --- what the requests ask the model for -----------------------------------------------

ANSWER_ONLY = {"reasoning": {"enabled": False}}


def declared(settings: list) -> list[tuple[int | None, object]]:
    return [((s or {}).get("max_tokens"), (s or {}).get("extra_body")) for s in settings]


async def test_the_one_request_localizer_asks_for_the_answer_only_within_a_cap_for_its_lines(
    temporal_client: Client,
) -> None:
    from localization.agents.answer_only import answer_cap

    model = OneRequestModel(answers=(fits,))
    handle, result = await run(temporal_client, LocalizerFakes(mode="single", model=model))
    assert isinstance(result, LocalizationResult), result
    # The fake script has two segments.
    assert declared(model.settings) == [(answer_cap(2), ANSWER_ONLY)]


async def test_the_repair_request_is_held_to_the_same_declaration(
    temporal_client: Client,
) -> None:
    from localization.agents.answer_only import answer_cap

    model = OneRequestModel(
        answers=(lambda prompt: [{"id": 0, "text_vi": SHORT}], fits),
    )
    handle, result = await run(temporal_client, LocalizerFakes(mode="single", model=model))
    assert isinstance(result, LocalizationResult), result
    assert declared(model.settings) == [(answer_cap(2), ANSWER_ONLY)] * 2


async def test_the_one_request_dubber_asks_for_the_answer_only_within_a_cap_for_its_lines(
    temporal_client: Client,
) -> None:
    from localization.agents.answer_only import answer_cap

    model = OneRequestModel(answers=(shorter,))
    dubbing = DubFakes(unfit_ids=(0, 1), mode="single")
    dubbing.dubber = model  # type: ignore[assignment]
    handle, result = await run(temporal_client, LocalizerFakes(), dubbing)
    assert isinstance(result, LocalizationResult), result
    assert declared(model.settings) == [(answer_cap(2), ANSWER_ONLY)]


async def test_the_tool_calling_agents_send_the_plain_request(temporal_client: Client) -> None:
    model = ScriptedModel()
    dubbing = DubFakes(unfit_ids=(0, 1), mode="tools")
    handle, result = await run(temporal_client, LocalizerFakes(mode="tools", model=model), dubbing)
    assert isinstance(result, LocalizationResult), result
    assert model.settings and dubbing.dubber.settings
    for settings in (*model.settings, *dubbing.dubber.settings):
        assert "max_tokens" not in (settings or {}) and "extra_body" not in (settings or {})


async def test_recorded_review_policy_uses_single_context_and_replays(temporal_client):
    from temporalio import activity

    from localization.contracts import (
        LocalizedLines,
        LocalizedScriptRef,
        ScriptResult,
    )
    from localization.workflows.video_localization import VideoLocalizationWorkflow
    from tests.replay.test_replay import _replayer

    @activity.defn(name="assemble_script")
    async def assemble(
        spec: LocalizationInput,
        transcript_key: str,
        subtitle_key: str,
        lines: LocalizedLines,
        round: int = 0,
        version: int = 1,
    ) -> ScriptResult:
        assert [line.status for line in lines.lines] == ["inferred", "unresolved"]
        assert lines.lines[0].reason == "Context supports greeting"
        assert lines.lines[1].reason == "No source evidence"
        return ScriptResult(
            script=LocalizedScriptRef(script_key="work/7001/r0/script.v1.json", version=version),
            needs_human=True,
            flagged_segment_ids=(),
            inferred_segment_ids=(0,),
            unresolved_segment_ids=(1,),
        )

    model = OneRequestModel(
        answers=(
            lambda _: [
                {
                    "id": 0,
                    "text_vi": LONG,
                    "status": "inferred",
                    "reason": "Context supports greeting",
                },
                {"id": 1, "text_vi": "", "status": "unresolved", "reason": "No source evidence"},
            ],
            lambda _: [{"id": 0, "text_vi": SHORT}],
        )
    )
    # Review policy explicitly selects contextual single mode even on a tools worker.
    localizing = LocalizerFakes(mode="tools", model=model, assemble=assemble)
    handle, result = await run(
        temporal_client,
        localizing,
        spec=make_input().model_copy(update={"delivery_mode": "review_project"}),
        workflow_type=BeforeReviewProjectsWorkflow,
    )
    assert isinstance(result, LocalizationResult), result
    assert model.requests == 2 and not model.tools_offered
    assert result.publish.outcome == "needs_human"
    names = await scheduled(handle)
    assert names.count("localizer_context") == 1
    assert not any(n.startswith("agent__localizer__") for n in names)
    history = await handle.fetch_history()
    assert any(b"contextual-localizer-unresolved" in e.SerializeToString() for e in history.events)
    await _replayer(VideoLocalizationWorkflow).replay_workflow(history)


async def test_contextual_missing_id_cannot_become_an_implicit_omission(temporal_client):
    model = OneRequestModel(answers=(lambda _: [{"id": 0, "text_vi": SHORT}],))
    localizing = LocalizerFakes(mode="single", model=model)
    _, result = await run(
        temporal_client,
        localizing,
        spec=make_input().model_copy(update={"delivery_mode": "review_project"}),
        workflow_type=BeforeReviewProjectsWorkflow,
    )
    assert not isinstance(result, LocalizationResult)
    assert localizing.calls["assemble_script"] == 0
    assert model.requests == 2
