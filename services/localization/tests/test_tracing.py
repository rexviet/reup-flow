from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from localization.config import Settings
from localization.observability.agent_tracing import agent_instrumentation, instrument_agents
from localization.observability.span_redaction import RedactingExporter
from localization.observability.temporal_tracing import RunTracingInterceptor, video_of
from localization.observability.tracing import (
    SESSION_ATTRIBUTE,
    WorkflowSafeTracerProvider,
    configured_secrets,
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


def test_the_interceptor_is_the_run_tracing_one_and_attached_on_the_client() -> None:
    assert isinstance(tracing_interceptor(), RunTracingInterceptor)
    import inspect

    from localization.temporal import client

    assert "tracing_interceptor()" in inspect.getsource(client.connect)


def test_the_provider_hands_out_temporals_replay_safe_tracers_and_still_flushes() -> None:
    exporter = InMemorySpanExporter()
    provider = WorkflowSafeTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    assert type(tracer).__name__ == "_ReplaySafeTracer"
    with tracer.start_as_current_span("outside a workflow"):
        pass
    assert provider.force_flush()
    assert [s.name for s in exporter.get_finished_spans()] == ["outside a workflow"]


def test_with_langfuse_configured_spans_leave_through_the_redacting_exporter(monkeypatch) -> None:  # noqa: ANN001
    import localization.observability.tracing as tracing

    installed = []
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", installed.append)
    settings = Settings(
        langfuse_host="http://langfuse-web:3000",
        langfuse_public_key="pk-lf-1",
        langfuse_secret_key="sk-lf-1",
        openrouter_api_key="sk-or-canary-0001",  # type: ignore[arg-type]
    )
    provider = init_tracing(settings, "t")
    assert isinstance(provider, WorkflowSafeTracerProvider) and installed == [provider]
    (processor,) = provider._active_span_processor._span_processors  # noqa: SLF001
    exporter = processor._batch_processor._exporter  # noqa: SLF001
    assert isinstance(exporter, RedactingExporter)
    assert "sk-or-canary-0001" in exporter._secrets  # noqa: SLF001
    provider.shutdown()


def test_every_credential_of_the_settings_is_a_secret_to_redact() -> None:
    settings = Settings(
        openrouter_api_key="sk-or-1234567890",  # type: ignore[arg-type]
        voicestudio_api_key="vs-1234567890",  # type: ignore[arg-type]
        vbee_voice_id="voice-1234567890",  # type: ignore[arg-type]
        loc_database_url="postgresql://u:pw-123456@db/x",  # type: ignore[arg-type]
    )
    assert set(configured_secrets(settings)) == {
        "sk-or-1234567890",
        "vs-1234567890",
        "voice-1234567890",
        "postgresql://u:pw-123456@db/x",
    }


def test_the_agents_never_record_binary_content_and_follow_the_content_switch() -> None:
    from pydantic_ai import Agent

    import localization.observability.agent_tracing as tracing

    recorded = tracing._record_content  # noqa: SLF001
    try:
        for content in (True, False):
            instrument_agents(TracerProvider(), include_content=content)
            settings = Agent._instrument_default  # noqa: SLF001
            assert settings.include_content is content
            assert settings.include_binary_content is False
            # The Reviewer, built per review, follows the same switch.
            reviewer = agent_instrumentation()
            assert reviewer.include_content is content
            assert reviewer.include_binary_content is False
    finally:
        Agent.instrument_all(False)
        tracing._record_content = recorded  # noqa: SLF001


def test_content_capture_is_on_unless_switched_off(monkeypatch) -> None:  # noqa: ANN001
    assert Settings().trace_agent_content is True
    monkeypatch.setenv("TRACE_AGENT_CONTENT", "false")
    assert Settings().trace_agent_content is False


def test_the_run_is_its_videos_session() -> None:
    assert video_of("video-7001-g3") == "7001"
    assert video_of("video-7001-g1-6f1e") == "7001"
    assert video_of("canary-1") is None
