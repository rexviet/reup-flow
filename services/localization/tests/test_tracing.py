from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.contrib.opentelemetry import TracingInterceptor

from localization.config import Settings
from localization.observability.tracing import (
    SESSION_ATTRIBUTE,
    init_tracing,
    langfuse_exporter,
    langfuse_headers,
    tag_session,
    tracing_interceptor,
)


def test_the_session_tag_lands_on_the_current_span_as_the_video_id() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("t").start_as_current_span("activity"):
        tag_session("7001")
    (span,) = exporter.get_finished_spans()
    # The literal, not the constant: Langfuse reads exactly this attribute name.
    assert SESSION_ATTRIBUTE == "session.id"
    assert span.attributes["session.id"] == "7001"


def test_the_exporter_targets_langfuse_otlp_with_basic_auth(monkeypatch) -> None:  # noqa: ANN001
    settings = Settings(
        langfuse_host="http://langfuse-web:3000/",
        langfuse_public_key="pk-lf-1",
        langfuse_secret_key="sk-lf-1",
    )
    exporter = langfuse_exporter(settings)
    assert exporter is not None
    assert exporter._endpoint == "http://langfuse-web:3000/api/public/otel/v1/traces"
    # base64("pk-lf-1:sk-lf-1")
    assert langfuse_headers(settings)["Authorization"] == "Basic cGstbGYtMTpzay1sZi0x"


def test_without_langfuse_settings_there_is_no_exporter_and_no_provider() -> None:
    assert langfuse_exporter(Settings()) is None
    assert init_tracing(Settings(), "t") is None


def test_the_interceptor_is_temporals_and_attached_on_the_client() -> None:
    assert isinstance(tracing_interceptor(), TracingInterceptor)
    import inspect

    from localization.temporal import client

    assert "tracing_interceptor()" in inspect.getsource(client.connect)
