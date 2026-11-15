"""What a reader of the trace sees of an agent: the Dubber that writes code, inside the
real workflow on a local Temporal server, traced as a worker with Langfuse configured
traces it.

The tree is the agent run, its model requests (with the messages in and out, and the cost
OpenRouter reported), the `run_code` call with the code as its input, and under it each
tool call the code made. No span of Temporal's own wraps them. Each model request is one
observation carrying tokens and cost. A worker that restarts mid-run replays the run and
exports no span twice. With content capture off, no prompt, answer, code or argument is
in any span, and the cost stays.

The model is scripted (`CodeDubber`); each of its requests also gets a real priced reply
from a local stub through the production OpenRouter client, so the cost travels the way a
real request's does: read by the response hook in the activity, returned with the answer,
written on the request's span in workflow code.
"""

import asyncio
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent, ModelResponse
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHistory
from temporalio.worker import Replayer

from localization.agents.dubber import CODE_INSTRUCTIONS
from localization.contracts import LocalizationResult
from localization.observability.agent_tracing import agent_instrumentation
from localization.observability.llm_usage import COST_DETAILS, OBSERVATION_TYPE, USAGE_DETAILS
from localization.observability.reported_cost import ReportedCostModel
from localization.observability.tracing import tracing_interceptor
from localization.providers.llm import openrouter_client
from localization.temporal.client import guarded_data_converter
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.observability.test_llm_cost import completion
from tests.support.dub_fakes import CodeDubber, DubFakes, FakeDubberPorts
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.spans import exported_spans, instrumented_agents
from tests.support.stub_server import Scenario, StubServer
from tests.workflows.test_dubbing_stage import names, scheduled

MODEL_REQUEST = "agent__dubber_code__model_request"
HISTORIES = Path(__file__).resolve().parents[1] / "replay" / "histories"
# What the run says and does, each in a different place: the instructions, the prompt,
# the code (and the arguments of the calls it makes), a tool's result, the answer.
CONTENT = {
    "instructions": CODE_INSTRUCTIONS.splitlines()[0],
    "prompt": "Shorten these lines so they fit their slots",
    "code": "asyncio.gather(*[synthesize(id=i",
    "tool result": "câu dài số 0",
    "answer": "câu ngắn 0",
}


class PricedReplies(WrapperModel):
    """The scripted model, with each request also answered by a priced OpenRouter reply
    through the production client, whose generation id the response then carries."""

    def __init__(self, wrapped: Any, stub: StubServer, http: httpx.AsyncClient) -> None:
        super().__init__(wrapped)
        self.stub = stub
        self.http = http
        self.costs: dict[str, float] = {}

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        generation = f"gen-{uuid.uuid4().hex[:8]}"
        cost = round(0.0001 * (len(self.costs) + 1) + 0.00003, 6)
        self.costs[generation] = cost
        self.stub.scenario = Scenario(json_body=completion(generation, cost))
        await self.http.post(f"{self.stub.url}/chat/completions", json={})
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        return replace(response, provider_response_id=generation)


@dataclass
class PricedDubber:
    """A `CodeDubber` bound the way the worker binds the Dubber's model."""

    stub: StubServer
    http: httpx.AsyncClient
    script: CodeDubber = field(default_factory=CodeDubber)
    priced: PricedReplies | None = None

    def model(self) -> ReportedCostModel:
        self.priced = PricedReplies(self.script.model(), self.stub, self.http)
        return ReportedCostModel(self.priced)


@pytest.fixture
async def priced(stub: StubServer):  # noqa: ANN201
    async with openrouter_client() as http:
        yield PricedDubber(stub, http)


def _start(client: Client):  # noqa: ANN202
    return client.start_workflow(
        "VideoLocalizationWorkflow",
        make_input(),
        id=f"video-7001-g1-{uuid.uuid4()}",
        task_queue="workflows",
        result_type=LocalizationResult,
    )


async def _run(client: Client, dubbing: DubFakes, include_content: bool = True):  # noqa: ANN202
    with exported_spans() as exporter, instrumented_agents(include_content=include_content):
        async with prep_workers(client, PrepFakes(), LocalizerFakes(), dubbing=dubbing):
            handle = await _start(client)
            result = await asyncio.wait_for(handle.result(), timeout=60)
        return handle, result, list(exporter.get_finished_spans())


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _children(spans: list[ReadableSpan], parent: ReadableSpan) -> list[ReadableSpan]:
    return [s for s in spans if s.parent and s.parent.span_id == parent.context.span_id]


def _text(spans: list[ReadableSpan]) -> str:
    """Every attribute and event of every span, as one string."""
    return " ".join(
        str(_attrs(s)) + str([(e.name, dict(e.attributes or {})) for e in s.events]) for s in spans
    )


def _one(spans: list[ReadableSpan], name: str) -> ReadableSpan:
    found = [s for s in spans if s.name == name]
    assert len(found) == 1, f"{name}: {len(found)} spans"
    return found[0]


async def test_the_dubber_that_writes_code_is_one_readable_tree(
    temporal_client: Client, priced: PricedDubber
) -> None:
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=FakeDubberPorts(), dubber=priced)
    handle, result, spans = await _run(temporal_client, dubbing)
    assert isinstance(result, LocalizationResult), result
    requests = names(await scheduled(handle)).count(MODEL_REQUEST)
    assert requests == 2

    # One trace, rooted at the run, with no span of Temporal's own around the agent.
    root = _one(spans, "RunWorkflow:VideoLocalizationWorkflow")
    assert root.parent is None and _attrs(root)["session.id"] == "7001"
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    assert not [s.name for s in spans if s.name.startswith(("Start", "Complete"))]
    assert not [s.name for s in spans if s.name.startswith("RunActivity:agent__")]

    run = _one(spans, "invoke_agent dubber_code")
    assert run.parent is not None and run.parent.span_id == root.context.span_id
    assert "câu ngắn 0" in _attrs(run)["final_result"]
    under_run = Counter(s.name for s in _children(spans, run))
    assert under_run == {"chat dubber-model": requests, "execute_tool run_code": 1}

    # Each model request: the messages in and out, its tokens, and the cost reported for it.
    chats = [s for s in _children(spans, run) if s.name == "chat dubber-model"]
    costs = assert_priced(chats, priced)
    assert sorted(costs) == sorted(priced.priced.costs.values())  # type: ignore[union-attr]
    assert "Shorten these lines" in _attrs(chats[0])["gen_ai.input.messages"]
    assert "run_code" in _attrs(chats[0])["gen_ai.output.messages"]
    # Nothing else in the trace carries a cost or a token count for these requests.
    priced_spans = [s for s in spans if COST_DETAILS in _attrs(s)]
    assert priced_spans == chats

    # The code is the input of `run_code`; what it returned is its output.
    run_code = _one(spans, "execute_tool run_code")
    code = json.loads(_attrs(run_code)["gen_ai.tool.call.arguments"])["code"]
    assert "asyncio.gather(*[synthesize(id=i" in code
    assert "segment 0" in _attrs(run_code)["gen_ai.tool.call.result"]
    # Each call the code made is a tool span under it, with its arguments and result.
    calls = _children(spans, run_code)
    assert Counter(s.name for s in calls) == {
        "execute_tool get_segment": 2,
        "execute_tool synthesize": 2,
        "execute_tool accept": 2,
    }
    spoken = [
        json.loads(_attrs(s)["gen_ai.tool.call.arguments"]) for s in calls if "synth" in s.name
    ]
    assert sorted(a["text_vi"] for a in spoken) == ["câu ngắn 0", "câu ngắn 1"]
    # 900 ms spoken in a 1000 ms slot: what the tool told the code.
    assert all(
        "fits without a speed-up" in _attrs(s)["gen_ai.tool.call.result"]
        for s in calls
        if "synth" in s.name
    )
    for marker in CONTENT.values():
        assert marker in _text(spans), marker


def assert_priced(chats: list[ReadableSpan], priced: PricedDubber) -> list[float]:
    """Each request span is one Langfuse generation with its tokens and reported cost."""
    costs = []
    for chat in chats:
        attrs = _attrs(chat)
        assert attrs[OBSERVATION_TYPE] == "generation"
        # The tokens of the response the span describes (the scripted model's, here).
        tokens = (attrs["gen_ai.usage.input_tokens"], attrs["gen_ai.usage.output_tokens"])
        assert json.loads(attrs[USAGE_DETAILS]) == {
            "input": tokens[0],
            "output": tokens[1],
            "total": sum(tokens),
        }
        cost = json.loads(attrs[COST_DETAILS])
        generation = attrs["gen_ai.response.id"]
        assert cost == {
            "upstream_inference_cost": 0.0004,
            "total": priced.priced.costs[generation],  # type: ignore[union-attr]
        }
        costs.append(cost["total"])
    return costs


async def test_with_content_capture_off_no_span_says_what_the_agent_said_or_did(
    temporal_client: Client, priced: PricedDubber
) -> None:
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=FakeDubberPorts(), dubber=priced)
    handle, result, spans = await _run(temporal_client, dubbing, include_content=False)
    assert isinstance(result, LocalizationResult), result
    text = _text(spans)
    assert [name for name, marker in CONTENT.items() if marker in text] == []
    # The tree, the tokens and the costs are all still there.
    run_code = _one(spans, "execute_tool run_code")
    assert len(_children(spans, run_code)) == 6
    chats = [s for s in spans if s.name == "chat dubber-model"]
    assert len(chats) == names(await scheduled(handle)).count(MODEL_REQUEST) == 2
    assert len(assert_priced(chats, priced)) == 2


# --- replay -------------------------------------------------------------------------------


def _replayer() -> Replayer:
    """A replayer configured as the workers are: the tracing interceptor and the agents
    traced on the replay-safe provider."""
    return Replayer(
        workflows=[VideoLocalizationWorkflow],
        data_converter=guarded_data_converter(),
        plugins=[PydanticAIPlugin()],
        interceptors=[tracing_interceptor()],
    )


@pytest.mark.parametrize(
    "path", sorted((HISTORIES / "dubber-code-mode").glob("*.json")), ids=lambda p: p.stem
)
async def test_replaying_a_recorded_run_with_tracing_on_exports_no_span(path: Path) -> None:
    history = WorkflowHistory.from_json(f"replay-{path.stem}", path.read_text())
    with exported_spans() as exporter, instrumented_agents():
        await _replayer().replay_workflow(history)
        assert exporter.get_finished_spans() == ()


async def test_control_a_provider_that_is_not_replay_safe_exports_the_replayed_spans() -> None:
    """The replay above exports nothing because the spans are made by the replay-safe
    tracer, not because nothing was traced: the same replay with the agents on a plain
    provider exports every span the run made, again."""
    path = HISTORIES / "dubber-code-mode" / "dubber-writes-code.json"
    history = WorkflowHistory.from_json("replay-control", path.read_text())
    plain = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(plain))
    with exported_spans(), instrumented_agents():
        Agent.instrument_all(agent_instrumentation(provider))
        await _replayer().replay_workflow(history)
    assert "execute_tool run_code" in {s.name for s in plain.get_finished_spans()}


async def test_a_run_replayed_after_its_workers_restart_exports_each_span_once(
    temporal_client: Client, priced: PricedDubber
) -> None:
    """The workers stop while a line is being spoken and new ones finish the run: they
    rebuild it from its history, reopening the spans the first ones had open. Every span
    reaches the exporter once, and the agent's tree is whole."""
    ports = FakeDubberPorts(speak_seconds=1.0)
    dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=ports, dubber=priced)
    with exported_spans() as exporter, instrumented_agents():
        async with prep_workers(temporal_client, PrepFakes(), LocalizerFakes(), dubbing=dubbing):
            handle = await _start(temporal_client)
            while ports.calls["speak"] == 0:
                await asyncio.sleep(0.05)
        # The first workers are gone (the workflow worker first) and the run is not done:
        # only new workers, whose cache is empty, can finish it, by replaying its history.
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
        async with prep_workers(temporal_client, PrepFakes(), LocalizerFakes(), dubbing=dubbing):
            result = await asyncio.wait_for(handle.result(), timeout=60)
        spans = list(exporter.get_finished_spans())
    assert isinstance(result, LocalizationResult), result

    ids = Counter((s.context.trace_id, s.context.span_id) for s in spans)
    assert [s.name for s in spans if ids[(s.context.trace_id, s.context.span_id)] > 1] == []
    run = _one(spans, "invoke_agent dubber_code")
    run_code = _one(spans, "execute_tool run_code")
    assert run_code.parent is not None and run_code.parent.span_id == run.context.span_id
    assert Counter(s.name for s in _children(spans, run_code)) == {
        "execute_tool get_segment": 2,
        "execute_tool synthesize": 2,
        "execute_tool accept": 2,
    }
    requests = names(await scheduled(handle)).count(MODEL_REQUEST)
    assert len([s for s in _children(spans, run) if s.name == "chat dubber-model"]) == requests
    assert len(ports.spoken) == 2
