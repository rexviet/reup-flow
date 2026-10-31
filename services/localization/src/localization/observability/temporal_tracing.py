"""How a run becomes one trace: the trace context crosses Temporal in headers, and only
the spans a reader needs are made.

Temporal's own interceptors wrap every call in spans of their own (`StartWorkflow`,
`StartActivity`, `RunActivity`, `CompleteWorkflow`). Around an agent those wrappers carry
no data: each model request and each tool call was a flat pair of empty spans, and with
pydantic-ai's spans (which hold the prompt, the code, the arguments) they would only bury
that tree. This interceptor makes two kinds of span and no others:

- `RunWorkflow:<type>`, the root of the run's trace, opened in workflow code by the
  replay-safe tracer (see `tracing.WorkflowSafeTracerProvider`), so a replay neither
  duplicates it nor changes its id. It carries the video as the Langfuse session.
- `RunActivity:<type>` for each activity that is a step of the run (ingest, transcribe,
  publish...). Provider spans and the cost of a request made there land under it.
- Nothing for the activities a durable agent runs for itself (`agent__...`: its model
  requests and tool calls). pydantic-ai's own `chat` and `execute_tool` spans, made in
  workflow code, stand for them, and what the activity does (a TTS call) nests under
  that span because the header carries it as the parent.

No span is ever made for starting an activity or a workflow: those only carry the
context, in the header Temporal's own interceptor used (`_tracer-data`, W3C trace
context), so an activity scheduled before this was deployed still finds its parent.
"""

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, NoReturn

import opentelemetry.context
from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import Context
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from temporalio import activity, client, converter, workflow
from temporalio.api.common.v1 import Payload
from temporalio.worker import (
    ActivityInboundInterceptor,
    ContinueAsNewInput,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    StartActivityInput,
    StartChildWorkflowInput,
    StartLocalActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from localization.observability.tracing import SESSION_ATTRIBUTE

# OpenTelemetry picks its context implementation lazily, by reading entry points with
# `open()`, which the workflow sandbox forbids. Touch it here, at import, outside it.
opentelemetry.context.get_current()

HEADER = "_tracer-data"
# The activities a pydantic-ai durable agent registers are named `agent__<name>__...`.
AGENT_ACTIVITY_PREFIX = "agent__"
_VIDEO_WORKFLOW_ID = re.compile(r"video-([0-9]{1,32})-g[0-9]+(?:-.*)?")
_PROPAGATOR = CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
_TRACER_NAME = "localization.temporal"


def _tracer() -> trace.Tracer:
    # Asked each time: the global provider is installed after this module is imported.
    return trace.get_tracer(_TRACER_NAME)


def _inject(headers: Mapping[str, Payload]) -> Mapping[str, Payload]:
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    if not carrier:
        return headers
    return {**headers, HEADER: converter.PayloadConverter.default.to_payloads([carrier])[0]}


def _extract(headers: Mapping[str, Payload]) -> Context:
    header = headers.get(HEADER)
    if header is None:
        return Context()
    carrier = converter.PayloadConverter.default.from_payloads([header])[0]
    return _PROPAGATOR.extract(carrier) if isinstance(carrier, dict) else Context()


@contextmanager
def _attached(context: Context) -> Iterator[None]:
    token = opentelemetry.context.attach(context)
    try:
        yield
    finally:
        # A workflow's coroutine may finish in another context; detaching a token that is
        # not the current one only logs an error, so it is skipped then, as Temporal does.
        if opentelemetry.context.get_current() is context:
            opentelemetry.context.detach(token)


def video_of(workflow_id: str) -> str | None:
    """The video (aweme id) a localization workflow id names, or None."""
    match = _VIDEO_WORKFLOW_ID.fullmatch(workflow_id)
    return match[1] if match else None


class RunTracingInterceptor(client.Interceptor, Interceptor):
    """On the client, so the workers built from it inherit it (see `temporal.client`)."""

    def intercept_client(self, next: client.OutboundInterceptor) -> client.OutboundInterceptor:
        return _ClientOutbound(next)

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _ActivityInbound(next)

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        return _WorkflowInbound


class _ClientOutbound(client.OutboundInterceptor):
    async def start_workflow(self, input: client.StartWorkflowInput) -> Any:
        input.headers = _inject(input.headers)
        return await super().start_workflow(input)

    async def signal_workflow(self, input: client.SignalWorkflowInput) -> None:
        input.headers = _inject(input.headers)
        await super().signal_workflow(input)


class _ActivityInbound(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        with _attached(_extract(input.headers)):
            info = activity.info()
            if info.activity_type.startswith(AGENT_ACTIVITY_PREFIX):
                return await super().execute_activity(input)
            with _tracer().start_as_current_span(
                f"RunActivity:{info.activity_type}",
                kind=SpanKind.SERVER,
                attributes={
                    "temporalWorkflowID": info.workflow_id or "",
                    "temporalRunID": info.workflow_run_id or "",
                    "temporalActivityID": info.activity_id,
                },
            ):
                return await super().execute_activity(input)


class _WorkflowInbound(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_WorkflowOutbound(outbound))

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        info = workflow.info()
        attributes = {"temporalWorkflowID": info.workflow_id, "temporalRunID": info.run_id}
        video = video_of(info.workflow_id)
        if video is not None:
            attributes[SESSION_ATTRIBUTE] = video
        with (
            _attached(_extract(input.headers)),
            _tracer().start_as_current_span(
                f"RunWorkflow:{info.workflow_type}", kind=SpanKind.SERVER, attributes=attributes
            ),
        ):
            return await super().execute_workflow(input)


class _WorkflowOutbound(WorkflowOutboundInterceptor):
    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle:
        input.headers = _inject(input.headers)
        return super().start_activity(input)

    def start_local_activity(self, input: StartLocalActivityInput) -> workflow.ActivityHandle:
        input.headers = _inject(input.headers)
        return super().start_local_activity(input)

    async def start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> workflow.ChildWorkflowHandle:
        input.headers = _inject(input.headers)
        return await super().start_child_workflow(input)

    def continue_as_new(self, input: ContinueAsNewInput) -> NoReturn:
        input.headers = _inject(input.headers)
        super().continue_as_new(input)
