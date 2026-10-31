"""Tracing: the replay-safe tracer provider, the OTLP exporter to Langfuse, the Temporal
interceptor and the session tag.

The agents are traced by pydantic-ai's own instrumentation, content included (see
`agent_tracing`): a run shows the prompt and the answer of every model request, the code
the Dubber writes and every tool call with its arguments and result. Much of it runs in
workflow code (the agent run, its model requests and its tools are spans made there), so
the provider is Temporal's replay-safe one: while a workflow replays its history the
spans it opens again are neither exported a second time nor given new ids. The exporter
removes any configured credential from a span before it leaves the process (see
`span_redaction`). Spans a worker had finished but not yet sent when it was killed are
lost: the batch is sent every half second, and nothing is written to disk.
"""

import base64
import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SynchronousMultiSpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic import SecretStr
from temporalio.contrib.opentelemetry import create_tracer_provider

from localization.config import Settings
from localization.observability.span_redaction import RedactingExporter

log = logging.getLogger(__name__)

SESSION_ATTRIBUTE = "session.id"
_OTLP_PATH = "/api/public/otel/v1/traces"


class WorkflowSafeTracerProvider(TracerProvider):
    """An SDK provider (so `force_flush`, `shutdown` and `add_span_processor` work as
    usual) whose tracers are Temporal's replay-safe ones, built on the same processors.

    Inside a workflow such a tracer takes span ids from the workflow's deterministic
    random and skips ending a span while history is replayed; outside one it is an
    ordinary tracer. Temporal marks it experimental."""

    def __init__(self, resource: Resource | None = None) -> None:
        processors = SynchronousMultiSpanProcessor()
        super().__init__(resource=resource, active_span_processor=processors)
        self._replay_safe = create_tracer_provider(
            resource=self.resource, active_span_processor=processors, shutdown_on_exit=False
        )

    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
        return self._replay_safe.get_tracer(*args, **kwargs)


def tracing_interceptor() -> Any:
    """Attached to the client; workers built from that client inherit it."""
    # Imported here: the interceptor module reads this one's session attribute.
    from localization.observability.temporal_tracing import RunTracingInterceptor

    return RunTracingInterceptor()


def langfuse_headers(settings: Settings) -> dict[str, str] | None:
    public, secret = settings.langfuse_public_key, settings.langfuse_secret_key
    if not (settings.langfuse_host and public and secret):
        return None
    token = base64.b64encode(f"{public.get_secret_value()}:{secret.get_secret_value()}".encode())
    return {
        "Authorization": f"Basic {token.decode()}",
        "x-langfuse-ingestion-version": "4",
    }


def langfuse_exporter(settings: Settings) -> OTLPSpanExporter | None:
    headers = langfuse_headers(settings)
    if headers is None or not settings.langfuse_host:
        return None
    return OTLPSpanExporter(
        endpoint=f"{settings.langfuse_host.rstrip('/')}{_OTLP_PATH}", headers=headers
    )


def configured_secrets(settings: Settings) -> list[str]:
    """Every credential the settings hold, to be kept out of what is exported."""
    return [
        value.get_secret_value()
        for _, value in settings
        if isinstance(value, SecretStr) and value.get_secret_value()
    ]


def init_tracing(settings: Settings, service_name: str) -> TracerProvider | None:
    """Installs the global tracer provider. Returns None, and says so, when
    Langfuse is not configured: spans are then dropped, never sent elsewhere."""
    exporter = langfuse_exporter(settings)
    if exporter is None:
        log.warning("LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY unset; tracing off")
        return None
    provider = WorkflowSafeTracerProvider(Resource.create({"service.name": service_name}))
    # Batch, never Simple: a span ending inside the Temporal sandbox would export
    # synchronously there, where the sandbox forbids the I/O and drops the span.
    provider.add_span_processor(
        BatchSpanProcessor(
            RedactingExporter(exporter, configured_secrets(settings)), schedule_delay_millis=500
        )
    )
    trace.set_tracer_provider(provider)
    return provider


def tag_session(aweme_id: str) -> None:
    """Groups every span of one video under one Langfuse session."""
    trace.get_current_span().set_attribute(SESSION_ATTRIBUTE, aweme_id)
