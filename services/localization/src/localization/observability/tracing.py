"""Tracing: the Temporal interceptor, the OTLP exporter to Langfuse, and the session tag.

Workflow code creates no spans of its own. A model request runs in an activity,
so its span is born there and cannot be duplicated when a workflow replays.
Nothing here turns on pydantic-ai's own instrumentation for that reason.
"""

import base64
import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from temporalio.contrib.opentelemetry import TracingInterceptor

from localization.config import Settings

log = logging.getLogger(__name__)

SESSION_ATTRIBUTE = "session.id"
_OTLP_PATH = "/api/public/otel/v1/traces"


def tracing_interceptor() -> TracingInterceptor:
    """Attached to the client; workers built from that client inherit it."""
    return TracingInterceptor()


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


def init_tracing(settings: Settings, service_name: str) -> TracerProvider | None:
    """Installs the global tracer provider. Returns None, and says so, when
    Langfuse is not configured: spans are then dropped, never sent elsewhere."""
    exporter = langfuse_exporter(settings)
    if exporter is None:
        log.warning("LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY unset; tracing off")
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    # Batch, never Simple: a span ending inside the Temporal sandbox would export
    # synchronously there, where the sandbox forbids the I/O and drops the span.
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=500))
    trace.set_tracer_provider(provider)
    return provider


def tag_session(aweme_id: str) -> None:
    """Groups every span of one video under one Langfuse session."""
    trace.get_current_span().set_attribute(SESSION_ATTRIBUTE, aweme_id)
