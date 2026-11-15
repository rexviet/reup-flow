"""No credential leaves the process in a span, even with content capture on.

The canary: an OpenRouter key that a careless gateway echoes back in an error body. With
content capture on, pydantic-ai records the error's message on the agent's spans, so the
key really reaches them (the control shows it); the exporter the workers use removes it.
"""

import uuid

import httpx
import pytest
from openai import AsyncOpenAI
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from localization.observability.agent_tracing import agent_instrumentation
from localization.observability.span_redaction import REDACTED, RedactingExporter


def _text(spans: tuple[ReadableSpan, ...]) -> str:
    return " ".join(
        f"{s.name} {dict(s.attributes or {})} "
        + str([(e.name, dict(e.attributes or {})) for e in s.events])
        for s in spans
    )


async def _echoing_run(key: str, provider: TracerProvider) -> None:
    """An agent, traced with content, whose provider answers 401 quoting the key."""

    def echo(request: httpx.Request) -> httpx.Response:
        quoted = request.headers["authorization"]
        return httpx.Response(401, json={"error": {"message": f"invalid key: {quoted}"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(echo)) as http:
        client = AsyncOpenAI(base_url="http://x/v1", api_key=key, http_client=http, max_retries=0)
        model = OpenAIChatModel("x/y", provider=OpenAIProvider(openai_client=client))
        agent = Agent(
            model,
            capabilities=[Instrumentation(agent_instrumentation(provider, include_content=True))],
        )
        with pytest.raises(ModelHTTPError):
            await agent.run("hello")


def _provider(exporter: InMemorySpanExporter, secrets: list[str] | None) -> TracerProvider:
    provider = TracerProvider()
    sink = exporter if secrets is None else RedactingExporter(exporter, secrets)
    provider.add_span_processor(SimpleSpanProcessor(sink))
    return provider


async def test_an_echoed_key_never_leaves_in_a_span() -> None:
    key = f"sk-or-canary-{uuid.uuid4()}"
    exporter = InMemorySpanExporter()
    await _echoing_run(key, _provider(exporter, [key]))
    text = _text(exporter.get_finished_spans())
    assert "invalid key" in text, "the error never reached a span: the check proves nothing"
    assert key not in text
    assert REDACTED in text


async def test_control_without_the_redacting_exporter_the_key_is_in_the_spans() -> None:
    key = f"sk-or-canary-{uuid.uuid4()}"
    exporter = InMemorySpanExporter()
    await _echoing_run(key, _provider(exporter, None))
    assert key in _text(exporter.get_finished_spans())


async def test_an_authorization_value_is_removed_even_when_it_is_not_a_known_secret() -> None:
    exporter = InMemorySpanExporter()
    provider = _provider(exporter, [])
    with provider.get_tracer("t").start_as_current_span(
        "call", attributes={"error": "upstream said: Authorization: Bearer abcdefgh12345678"}
    ) as span:
        span.add_event("retry", {"why": ["Basic cGstbGYtMTpzay1sZi0x", "fine"]})
    (exported,) = exporter.get_finished_spans()
    assert exported.attributes["error"] == f"upstream said: Authorization: Bearer {REDACTED}"
    assert exported.events[0].attributes["why"] == (f"Basic {REDACTED}", "fine")


def test_a_span_without_a_secret_is_exported_as_it_is() -> None:
    exporter = InMemorySpanExporter()
    provider = _provider(exporter, ["sk-or-canary-unused"])
    with provider.get_tracer("t").start_as_current_span("plain", attributes={"n": 3, "t": "ok"}):
        pass
    (exported,) = exporter.get_finished_spans()
    assert dict(exported.attributes) == {"n": 3, "t": "ok"}


def test_a_short_value_is_not_treated_as_a_secret() -> None:
    redactor = RedactingExporter(InMemorySpanExporter(), ["abc", "sk-or-long-secret"])
    assert redactor.redact("abc and sk-or-long-secret") == f"abc and {REDACTED}"
