"""The Localizer as a durable Temporal agent.

Each model request is an activity on the `llm` queue and each tool that does I/O is
an activity on the `io` queue, so a worker that dies mid-run resumes from the last
completed request or tool call without repeating either. `estimate_duration` is pure
and runs inside the workflow.

This module is imported by workflow code, so it holds configuration only: no
settings, no provider, no storage. The model and the tools' ports are supplied by
the worker that runs the activities (`bind_model_factory`, `localizer.bind_ports`).
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from localization.agents.localizer import IO_TOOLS, PURE_TOOLS, LocalizerDeps, build_localizer
from localization.contracts.models import LocalizedLines

LLM_QUEUE = "llm"
IO_QUEUE = "io"

# The name the workflow-side agent carries for its model. The activity that makes a
# request turns it into the configured model.
MODEL_ALIAS = "localizer-model"

_PERMANENT = ["PermanentProviderError", "PayloadTooLarge", "ArtifactMissingError"]

MODEL_ACTIVITY = ActivityConfig(
    task_queue=LLM_QUEUE,
    start_to_close_timeout=timedelta(minutes=5),
    heartbeat_timeout=timedelta(seconds=30),
    retry_policy=RetryPolicy(
        maximum_attempts=6,
        initial_interval=timedelta(seconds=5),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(minutes=2),
        non_retryable_error_types=_PERMANENT,
    ),
)
TOOL_ACTIVITY = ActivityConfig(
    task_queue=IO_QUEUE,
    start_to_close_timeout=timedelta(minutes=2),
    # The agent's activities heartbeat in the background, so a tool call whose worker
    # died is noticed in seconds and retried, not after the full timeout.
    heartbeat_timeout=timedelta(seconds=15),
    retry_policy=RetryPolicy(
        maximum_attempts=5,
        initial_interval=timedelta(seconds=2),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=30),
        non_retryable_error_types=_PERMANENT,
    ),
)
# Pure tools get no activity: they run in the workflow, instantly.
INLINE = False

TOOL_METADATA: dict[str, dict[str, object]] = {
    **{name: {"temporal": TOOL_ACTIVITY} for name in IO_TOOLS},
    **{name: {"temporal": INLINE} for name in PURE_TOOLS},
}

_model_factory: Callable[[], Model] | None = None


def bind_model_factory(factory: Callable[[], Model] | None) -> None:
    """Called by the worker that serves the `llm` queue."""
    global _model_factory
    _model_factory = factory


class DeferredModel(Model):
    """The model as workflow code sees it: a name, nothing more. Workflow code never
    sends a request (the durable agent turns each one into an activity), and it must
    not read settings or open a connection, so the real model cannot be built here.
    The activity that does send the request lands on this object and hands the
    request to the model the worker bound."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def system(self) -> str:
        return "openai"

    @staticmethod
    def _real() -> Model:
        if _model_factory is None:
            raise RuntimeError("a model request reached a worker that has no model bound")
        return _model_factory()

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        return await self._real().request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        async with self._real().request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as response:
            yield response


def build_durable_localizer() -> Agent[LocalizerDeps, LocalizedLines]:
    return build_localizer(
        MODEL_ALIAS,
        tool_metadata=TOOL_METADATA,
        capabilities=[
            TemporalDurability(
                models={MODEL_ALIAS: DeferredModel(MODEL_ALIAS)},
                model_activity_config=MODEL_ACTIVITY,
                activity_config=TOOL_ACTIVITY,
            )
        ],
    )


# Module level: Temporal needs the agent built before the workflow runs, so its
# activities can be registered on the workers.
localizer = build_durable_localizer()


# A runaway agent (a validator it can never satisfy, a tool it keeps calling) must
# end the run rather than spend without bound.
USAGE_LIMITS = UsageLimits(request_limit=60, tool_calls_limit=200)


async def run_localizer(deps: LocalizerDeps) -> LocalizedLines:
    """Runs the Localizer to completion and returns its lines. The prompt names only
    the ids to cover; the transcript itself is read through the tools."""
    last = deps.segment_count - 1
    result = await localizer.run(
        f"Localize this video's transcript into Vietnamese. The segment ids are 0 to {last}.",
        deps=deps,
        usage_limits=USAGE_LIMITS,
    )
    return result.output
