"""One process-wide tracer provider for tests that read the spans production code makes.

OpenTelemetry lets the global provider be set once per process; a second
`set_tracer_provider` is ignored with a warning. Every test that needs the global one
(the provider classes and Temporal's interceptor use it) goes through here, so the order
the test modules run in does not decide which of them sees its spans. It is the
replay-safe provider the workers install, so spans made in workflow code (the run's root,
the agents' spans) are made as they are in production.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent

from localization.observability import agent_tracing
from localization.observability.tracing import WorkflowSafeTracerProvider

_EXPORTER = InMemorySpanExporter()
_installed = False


def global_exporter() -> InMemorySpanExporter:
    global _installed
    if not _installed:
        provider = WorkflowSafeTracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)
        _installed = True
    return _EXPORTER


@contextmanager
def exported_spans() -> Iterator[InMemorySpanExporter]:
    """The exporter, cleared before and after the block, so what it holds inside the
    block are the spans finished there."""
    exporter = global_exporter()
    exporter.clear()
    try:
        yield exporter
    finally:
        exporter.clear()


@contextmanager
def instrumented_agents(*, include_content: bool = True) -> Iterator[InMemorySpanExporter]:
    """The agents traced as a worker with Langfuse configured traces them, on the global
    provider; undone after, so another test's agents are not traced."""
    exporter = global_exporter()
    recorded = agent_tracing._record_content  # noqa: SLF001
    agent_tracing.instrument_agents(trace.get_tracer_provider(), include_content=include_content)
    try:
        yield exporter
    finally:
        Agent.instrument_all(False)
        agent_tracing._record_content = recorded  # noqa: SLF001
