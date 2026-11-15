"""What a model request cost reaches the span and the database, from the response body.

A stub OpenAI-compatible server plays OpenRouter. The same HTTP client, with the same
response hook, serves a pydantic-ai agent and plain httpx (which is what OCR uses).
"""

import dataclasses
import json
import logging
import math
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent, ModelResponse, TextPart
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RequestUsage
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment

from localization.activities.publish import RunStore
from localization.observability.agent_tracing import agent_instrumentation
from localization.observability.llm_calls import LlmCallRecorder
from localization.observability.llm_usage import (
    COST_DETAILS,
    MODEL_NAME,
    OBSERVATION_TYPE,
    USAGE_DETAILS,
    LlmUsage,
    parse_usage,
)
from localization.observability.model_cost import (
    COST_DETAILS_KEY,
    CostOnRequestSpan,
    request_usage,
    with_reported_cost,
)
from localization.observability.reported_cost import ReportedCostModel
from localization.providers.llm import openrouter_client
from tests.support.scratch_db import scratch_database
from tests.support.stub_server import Scenario, StubServer

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
CORE = (MIGRATIONS / "261026-localization-core.sql").read_text()
LLM_COST = (MIGRATIONS / "261027-llm-cost.sql").read_text()
LLM_PROVIDER = (MIGRATIONS / "261028-llm-call-provider.sql").read_text()
API_KEY = "sk-or-canary-7f3a9"
PROMPT = "SECRET PROMPT the user wrote"
IMAGE = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD"


def completion(gen_id: str | None = "gen-1", cost: object = 0.00042, **usage: object) -> dict:
    body: dict = {
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek/probe",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, **usage},
    }
    if gen_id is not None:
        body["id"] = gen_id
    if cost is not None:
        body["usage"]["cost"] = cost
        body["usage"]["cost_details"] = {"upstream_inference_cost": 0.0004}
    return body


@pytest.fixture
def db() -> Iterator[tuple[str, psycopg.Connection]]:
    yield from scratch_database(CORE, LLM_COST, LLM_PROVIDER)


@pytest.fixture
def spans() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter


def in_activity(workflow_id: str = "video-7001-g3", activity_type: str = "review_render"):  # noqa: ANN201
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_id=workflow_id, activity_type=activity_type)
    return env


async def post(client: httpx.AsyncClient, stub: StubServer) -> httpx.Response:
    return await client.post(
        f"{stub.url}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": "deepseek/probe",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": IMAGE}},
                    ],
                }
            ],
        },
    )


def attributes_of(exporter: InMemorySpanExporter) -> dict:
    (span,) = exporter.get_finished_spans()
    return dict(span.attributes or {})


# --- the parser ---


def test_the_usage_block_is_read_as_the_provider_reports_it() -> None:
    usage = parse_usage(completion("gen-abc", 0.00952))
    assert usage is not None
    assert (usage.generation_id, usage.model) == ("gen-abc", "deepseek/probe")
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cost_usd) == (10, 2, 0.00952)
    assert usage.cost_details == {"upstream_inference_cost": 0.0004}


@pytest.mark.parametrize(
    "body",
    [None, [], "gen-1", 3, {}, {"usage": "free"}, {"usage": None}, {"id": 5}],
)
def test_a_body_that_is_not_a_completion_gives_nothing_and_no_crash(body: object) -> None:
    usage = parse_usage(body)
    assert usage is None or (usage.cost_usd is None and usage.generation_id is None)


@pytest.mark.parametrize("cost", ["free", True, None, -1, math.nan, math.inf, [], {"a": 1}])
def test_a_cost_that_is_not_a_sane_number_is_none_never_made_up(cost: object) -> None:
    usage = parse_usage({"id": "gen-1", "usage": {"cost": cost, "prompt_tokens": "x"}})
    assert usage is not None
    assert usage.cost_usd is None and usage.prompt_tokens is None


# --- the span ---


async def test_the_cost_reaches_the_span_in_the_form_langfuse_ingests(
    stub: StubServer, spans: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    provider, exporter = spans
    stub.scenario = Scenario(json_body=completion("gen-1", 0.00952))
    async with openrouter_client() as client:
        with provider.get_tracer("t").start_as_current_span("provider.ocr"):
            await post(client, stub)
    attrs = attributes_of(exporter)
    assert attrs[OBSERVATION_TYPE] == "generation"
    # The model that answered, as the response names it.
    assert attrs[MODEL_NAME]
    assert json.loads(attrs[USAGE_DETAILS]) == {"input": 10, "output": 2, "total": 12}
    assert json.loads(attrs[COST_DETAILS])["total"] == 0.00952


async def test_a_response_without_a_cost_sets_no_cost_and_does_not_crash(
    stub: StubServer, spans: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    provider, exporter = spans
    stub.scenario = Scenario(json_body=completion("gen-1", None))
    async with openrouter_client() as client:
        with provider.get_tracer("t").start_as_current_span("provider.ocr"):
            response = await post(client, stub)
    assert response.status_code == 200 and response.json()["id"] == "gen-1"
    attrs = attributes_of(exporter)
    assert COST_DETAILS not in attrs
    assert json.loads(attrs[USAGE_DETAILS])["input"] == 10  # what is known is still there


async def test_nothing_sensitive_is_recorded(
    stub: StubServer,
    spans: tuple[TracerProvider, InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, exporter = spans
    stub.scenario = Scenario(json_body=completion("gen-1", 0.001))
    caplog.set_level(logging.DEBUG)
    async with openrouter_client() as client:
        with provider.get_tracer("t").start_as_current_span("provider.ocr"):
            await post(client, stub)
    attrs = attributes_of(exporter)
    assert set(attrs) == {OBSERVATION_TYPE, MODEL_NAME, USAGE_DETAILS, COST_DETAILS}
    everything = json.dumps(attrs) + caplog.text
    for secret in (API_KEY, "Bearer", PROMPT, IMAGE, "base64"):
        assert secret not in everything


async def test_a_streamed_reply_is_left_alone(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: {}\n\n"
        )

    from localization.observability.llm_usage import make_response_hook

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"response": [make_response_hook(None)]},
    ) as client:
        with provider.get_tracer("t").start_as_current_span("provider.ocr"):
            async with client.stream("POST", "http://x/chat/completions") as response:
                chunks = [c async for c in response.aiter_bytes()]
    assert b"".join(chunks) == b"data: {}\n\n"
    assert attributes_of(exporter) == {}


# --- the agent library goes through the same client ---


async def test_a_pydantic_ai_agent_request_is_priced_by_the_same_hook(
    stub: StubServer, spans: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    provider, exporter = spans
    stub.scenario = Scenario(json_body=completion("gen-agent", 0.00952))
    async with openrouter_client() as client:
        model = OpenAIChatModel(
            "deepseek/probe",
            provider=OpenAIProvider(base_url=stub.url, api_key=API_KEY, http_client=client),
        )
        with provider.get_tracer("t").start_as_current_span("chat deepseek/probe"):
            result = await Agent(model).run("hello")
    assert result.output == "ok"
    attrs = attributes_of(exporter)
    assert json.loads(attrs[COST_DETAILS])["total"] == 0.00952
    assert attrs[OBSERVATION_TYPE] == "generation"


# --- a durable agent's request: the cost travels back with the answer ---


async def _reported_request(stub: StubServer, body: dict) -> ModelResponse:
    """One request of a durable agent's model, as the worker binds it, through the
    production client."""
    stub.scenario = Scenario(json_body=body)
    async with openrouter_client() as client:
        model = ReportedCostModel(
            OpenAIChatModel(
                "deepseek/probe",
                provider=OpenAIProvider(base_url=stub.url, api_key=API_KEY, http_client=client),
            )
        )
        return await model.request(
            [ModelRequest(parts=[UserPromptPart("hello")])], None, ModelRequestParameters()
        )


async def test_a_durable_agents_model_returns_the_cost_openrouter_reported(
    stub: StubServer,
) -> None:
    response = await _reported_request(stub, completion("gen-dur", 0.00952))
    assert response.provider_response_id == "gen-dur"
    assert response.usage.cost == Decimal("0.00952")
    assert response.provider_details[COST_DETAILS_KEY] == {"upstream_inference_cost": 0.0004}
    # It reaches the workflow as an activity result: the cost survives the payload.
    converter = pydantic_data_converter.payload_converter
    (payload,) = converter.to_payloads([response])
    (back,) = converter.from_payloads([payload], [ModelResponse])
    assert back.usage.cost == Decimal("0.00952")
    assert request_usage(back).cost_details == {"upstream_inference_cost": 0.0004}


async def test_a_reply_without_a_reported_cost_comes_back_without_one(stub: StubServer) -> None:
    response = await _reported_request(stub, completion("gen-free", None))
    assert response.usage.cost is None
    assert COST_DETAILS_KEY not in (response.provider_details or {})


def test_a_reply_for_another_generation_lends_the_response_no_cost() -> None:
    response = ModelResponse(parts=[TextPart("ok")], provider_response_id="gen-2")
    other = LlmUsage(generation_id="gen-1", cost_usd=0.5)
    assert with_reported_cost(response, [other]).usage.cost is None
    mine = LlmUsage(generation_id="gen-2", cost_usd=0.25)
    assert with_reported_cost(response, [other, mine]).usage.cost == Decimal("0.25")


def _answer(cost: Decimal | None) -> FunctionModel:
    async def respond(messages: list, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[TextPart("ok")],
            usage=RequestUsage(input_tokens=11, output_tokens=3, cost=cost),
            model_name="deepseek/probe",
            provider_response_id="gen-span",
            provider_details=(
                {COST_DETAILS_KEY: {"upstream_inference_cost": 0.0004}} if cost else None
            ),
        )

    return FunctionModel(respond)


async def test_the_reported_cost_is_written_on_the_requests_own_span(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans
    agent = Agent(
        _answer(Decimal("0.00952")),
        capabilities=[
            Instrumentation(agent_instrumentation(provider, include_content=False)),
            CostOnRequestSpan(),
        ],
    )
    await agent.run("hello")
    (chat,) = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    attrs = dict(chat.attributes or {})
    assert attrs[OBSERVATION_TYPE] == "generation"
    # The model that answered, as the response names it.
    assert attrs[MODEL_NAME]
    assert json.loads(attrs[USAGE_DETAILS]) == {"input": 11, "output": 3, "total": 14}
    assert json.loads(attrs[COST_DETAILS]) == {"upstream_inference_cost": 0.0004, "total": 0.00952}
    # The agent's run span carries no cost: Langfuse would count it twice.
    others = [s for s in exporter.get_finished_spans() if s is not chat]
    assert others and not [s.name for s in others if COST_DETAILS in (s.attributes or {})]


async def test_a_request_with_no_reported_cost_gets_no_cost_on_its_span(
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = spans
    agent = Agent(
        _answer(None),
        capabilities=[Instrumentation(agent_instrumentation(provider)), CostOnRequestSpan()],
    )
    await agent.run("hello")
    (chat,) = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    attrs = dict(chat.attributes or {})
    assert COST_DETAILS not in attrs
    assert json.loads(attrs[USAGE_DETAILS]) == {"input": 11, "output": 3, "total": 14}


# --- the table and the run's total ---


def calls(conn: psycopg.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT generation_id, aweme_id, generation, stage, model, prompt_tokens, "
        "completion_tokens, cost_usd FROM localization.llm_calls ORDER BY generation_id"
    ).fetchall()


async def request(
    stub: StubServer, info: str, body: dict, workflow_id: str = "video-7001-g3"
) -> httpx.Response:
    stub.scenario = Scenario(json_body=body)
    async with openrouter_client(LlmCallRecorder(info)) as client:

        async def call() -> httpx.Response:
            return await post(client, stub)

        return await in_activity(workflow_id).run(call)


async def test_a_priced_request_leaves_a_row_for_its_video_generation_and_stage(
    stub: StubServer, db: tuple[str, psycopg.Connection]
) -> None:
    info, conn = db
    await request(stub, info, completion("gen-1", 0.00952))
    assert calls(conn) == [
        ("gen-1", "7001", 3, "review_render", "deepseek/probe", 10, 2, Decimal("0.00952000"))
    ]


async def test_the_provider_that_served_the_request_is_recorded(
    stub: StubServer, db: tuple[str, psycopg.Connection]
) -> None:
    """The same model answers in 5 s from one OpenRouter provider and in 40 s from
    another: the row says which one this was."""
    info, conn = db
    await request(stub, info, {**completion("gen-1"), "provider": "Relace"})
    await request(stub, info, completion("gen-2"))  # a body without one
    rows = conn.execute(
        "SELECT generation_id, provider FROM localization.llm_calls ORDER BY generation_id"
    ).fetchall()
    assert rows == [("gen-1", "Relace"), ("gen-2", None)]


async def test_the_runs_row_carries_the_sum_and_writing_it_again_changes_nothing(
    stub: StubServer, db: tuple[str, psycopg.Connection]
) -> None:
    info, conn = db
    await request(stub, info, completion("gen-1", 0.00952))
    await request(stub, info, completion("gen-2", 0.00123))
    await request(stub, info, completion("gen-other-run", 5.0))  # another video's request
    await request(stub, info, completion("gen-earlier", 7.0), "video-7001-g2")  # an earlier run
    conn.execute(
        "UPDATE localization.llm_calls SET aweme_id = '9999' WHERE generation_id = 'gen-other-run'"
    )
    runs = RunStore(info)

    async def write() -> None:
        await runs.upsert(
            aweme_id="7001",
            generation=3,
            workflow_id="video-7001-g3",
            status="published",
            output="output/7001.mp4",
            review=None,
            record={},
            review_rounds=1,
            needs_human=False,
        )

    for _ in range(2):  # idempotent
        await write()
        (cost,) = conn.execute("SELECT llm_cost_usd FROM localization.runs").fetchone()
        assert cost == Decimal("0.01075000")


async def test_a_run_with_no_reported_cost_has_no_total(
    stub: StubServer, db: tuple[str, psycopg.Connection]
) -> None:
    info, conn = db
    await request(stub, info, completion("gen-1", None))
    assert calls(conn)[0][7] is None
    await RunStore(info).upsert(
        aweme_id="7001", generation=3, workflow_id="w", status="failed", output=None,
        review=None, record={}, review_rounds=0, needs_human=False,
    )  # fmt: skip
    assert conn.execute("SELECT llm_cost_usd FROM localization.runs").fetchone() == (None,)


async def test_the_same_response_id_is_counted_once(
    stub: StubServer, db: tuple[str, psycopg.Connection]
) -> None:
    info, conn = db
    await request(stub, info, completion("gen-1", 0.00952))
    await request(stub, info, completion("gen-1", 0.00952))  # an activity retried
    assert len(calls(conn)) == 1
    (total,) = conn.execute("SELECT SUM(cost_usd) FROM localization.llm_calls").fetchone()
    assert total == Decimal("0.00952000")


async def test_a_request_outside_an_activity_is_not_recorded_but_is_priced_on_its_span(
    stub: StubServer,
    db: tuple[str, psycopg.Connection],
    spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    info, conn = db
    provider, exporter = spans
    stub.scenario = Scenario(json_body=completion("gen-1", 0.001))
    async with openrouter_client(LlmCallRecorder(info)) as client:
        with provider.get_tracer("t").start_as_current_span("x"):
            await post(client, stub)
    assert calls(conn) == []
    assert COST_DETAILS in attributes_of(exporter)


async def test_a_database_that_is_down_does_not_fail_the_request_and_the_log_has_no_password(
    stub: StubServer, caplog: pytest.LogCaptureFixture
) -> None:
    dead = "host=127.0.0.1 port=1 user=localization password=hunter2-canary dbname=reup"
    caplog.set_level(logging.DEBUG)
    response = await request(stub, dead, completion("gen-1", 0.001))
    assert response.status_code == 200 and response.json()["id"] == "gen-1"
    assert "could not record llm call gen-1" in caplog.text
    assert "hunter2-canary" not in caplog.text


# --- wiring ---------------------------------------------------------------------------


def test_the_agents_models_and_ocr_share_the_priced_client() -> None:
    from localization.config import Settings
    from localization.providers.llm import model_for
    from localization.providers.registry import build_providers

    settings = Settings(
        localizer_model="deepseek/probe",
        openrouter_api_key=API_KEY,
        ocr_model="deepseek/probe",
        ocr_bbox_format="xyxy_norm1000",
        voicestudio_url="https://voice.example",
        stt_model="s",
        tts_model="t",
    )
    client = openrouter_client()
    assert model_for("localizer", settings, client).client._client is client  # type: ignore[attr-defined]
    assert build_providers(settings, llm_http=client).ocr._client._http is client  # type: ignore[union-attr]


def test_the_cost_migration_adds_the_table_and_the_column_and_its_rollback_removes_them(
    db: tuple[str, psycopg.Connection],
) -> None:
    _, conn = db
    columns = lambda table: {  # noqa: E731
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'localization' AND table_name = %s",
            (table,),
        )
    }
    assert "llm_cost_usd" in columns("runs")
    assert {"generation_id", "aweme_id", "generation", "stage", "cost_usd"} <= columns("llm_calls")
    conn.execute((MIGRATIONS / "261027-llm-cost.down.sql").read_text())
    assert "llm_cost_usd" not in columns("runs") and columns("llm_calls") == set()
    assert {"glossary", "voice_rates", "runs"} <= {
        r[0]
        for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'localization'")
    }
