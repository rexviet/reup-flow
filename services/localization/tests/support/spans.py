"""One process-wide tracer provider for tests that read the spans production code makes.

OpenTelemetry lets the global provider be set once per process; a second
`set_tracer_provider` is ignored with a warning. Every test that needs the global one
(the provider classes and Temporal's interceptor use it) goes through here, so the order
the test modules run in does not decide which of them sees its spans.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_EXPORTER = InMemorySpanExporter()
_installed = False


def global_exporter() -> InMemorySpanExporter:
    global _installed
    if not _installed:
        provider = TracerProvider()
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
