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
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.models import (
    Model,
    ModelRequestContext,
    ModelRequestParameters,
    StreamedResponse,
)
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

from localization.agents import dubber as dubber_module
from localization.agents.dubber import DubberDeps, build_dubber
from localization.agents.localizer import IO_TOOLS, PURE_TOOLS, LocalizerDeps, build_localizer
from localization.contracts.limits import MAX_PAYLOAD_BYTES, payload_size
from localization.contracts.models import DubDecisions, LocalizedLines

LLM_QUEUE = "llm"
IO_QUEUE = "io"
TTS_QUEUE = "tts"

# The name the workflow-side agent carries for its model. The activity that makes a
# request turns it into the configured model.
MODEL_ALIAS = "localizer-model"
DUBBER_MODEL_ALIAS = "dubber-model"

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

_model_factories: dict[str, Callable[[], Model]] = {}


def bind_model_factory(factory: Callable[[], Model] | None, alias: str = MODEL_ALIAS) -> None:
    """Called by the worker that serves the `llm` queue, once per agent."""
    if factory is None:
        _model_factories.pop(alias, None)
    else:
        _model_factories[alias] = factory


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

    def _real(self) -> Model:
        factory = _model_factories.get(self._name)
        if factory is None:
            raise RuntimeError("a model request reached a worker that has no model bound")
        return factory()

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


# Every model request re-sends the whole conversation, and a conversation that grows
# past the payload limit does not fail the run: the codec raises while the workflow's
# commands are encoded, Temporal retries that task forever, and the run sits RUNNING.
# So the size is checked here, in the workflow, and an oversize conversation fails the
# run with a clear error instead. The margin leaves room for the rest of the request.
MAX_CONVERSATION_BYTES = MAX_PAYLOAD_BYTES * 3 // 4
# Measured: a request carrying 60 dense segments of speech (the three-minute case) is
# about 45 KB, i.e. under 1 KB a segment, and an answer is of the same order. This many
# segments keeps one read plus one wrong answer well inside the limit; a longer video
# needs the transcript read in windows.
MAX_SEGMENTS = 150


class ConversationSizeGuard(AbstractCapability[LocalizerDeps]):
    """Fails the run, non-retryably, before a request whose conversation is too big."""

    async def before_model_request(
        self, ctx: RunContext[LocalizerDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        size = payload_size(ModelMessagesTypeAdapter.dump_json(request_context.messages))
        if size > MAX_CONVERSATION_BYTES:
            raise ApplicationError(
                f"the Localizer conversation is {size} bytes, over the "
                f"{MAX_CONVERSATION_BYTES} byte limit; the transcript is too long to read in one "
                "piece",
                type="PayloadTooLarge",
                non_retryable=True,
            )
        return request_context


def build_durable_localizer() -> Agent[LocalizerDeps, LocalizedLines]:
    return build_localizer(
        MODEL_ALIAS,
        tool_metadata=TOOL_METADATA,
        capabilities=[
            ConversationSizeGuard(),
            TemporalDurability(
                models={MODEL_ALIAS: DeferredModel(MODEL_ALIAS)},
                model_activity_config=MODEL_ACTIVITY,
                activity_config=TOOL_ACTIVITY,
            ),
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
    if deps.segment_count > MAX_SEGMENTS:
        raise ApplicationError(
            f"the transcript has {deps.segment_count} segments, over the {MAX_SEGMENTS} the "
            "Localizer reads in one piece",
            type="PayloadTooLarge",
            non_retryable=True,
        )
    last = deps.segment_count - 1
    result = await localizer.run(
        f"Localize this video's transcript into Vietnamese. The segment ids are 0 to {last}.",
        deps=deps,
        usage_limits=USAGE_LIMITS,
    )
    return result.output


# --- the Dubber -------------------------------------------------------------------

# Speaking a line is a TTS call, so the tool runs on the `tts` queue: its concurrency
# limit covers the Dubber exactly as it covers the batch synthesis.
TTS_TOOL_ACTIVITY = ActivityConfig(
    task_queue=TTS_QUEUE,
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

DUBBER_TOOL_METADATA: dict[str, dict[str, object]] = {
    **{name: {"temporal": TOOL_ACTIVITY} for name in dubber_module.IO_TOOLS},
    **{name: {"temporal": TTS_TOOL_ACTIVITY} for name in dubber_module.TTS_TOOLS},
    **{name: {"temporal": INLINE} for name in dubber_module.PURE_TOOLS},
}


def build_durable_dubber() -> Agent[DubberDeps, DubDecisions]:
    return build_dubber(
        DUBBER_MODEL_ALIAS,
        tool_metadata=DUBBER_TOOL_METADATA,
        capabilities=[
            TemporalDurability(
                models={DUBBER_MODEL_ALIAS: DeferredModel(DUBBER_MODEL_ALIAS)},
                model_activity_config=MODEL_ACTIVITY,
                activity_config=TOOL_ACTIVITY,
            ),
        ],
    )


dubber = build_durable_dubber()

# The Dubber handles a few lines: a handful of requests each.
DUBBER_USAGE_LIMITS = UsageLimits(request_limit=120, tool_calls_limit=400)


async def run_dubber(deps: DubberDeps) -> DubDecisions:
    """Runs the Dubber to completion. The prompt names only the ids; the lines
    themselves are read through the tools."""
    result = await dubber.run(
        f"Shorten these lines so they fit their slots: segment ids {list(deps.pending_ids)}.",
        deps=deps,
        usage_limits=DUBBER_USAGE_LIMITS,
    )
    return result.output
