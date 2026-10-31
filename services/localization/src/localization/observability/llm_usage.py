"""What a model request cost, as the provider reports it.

OpenRouter puts `usage.cost` (USD) and `usage.cost_details` in every chat completion,
and the generation id (`gen-...`) at the top of the body. Nothing here estimates a
price: a response without a cost has no cost.

`make_response_hook` is attached to the one HTTP client both the OCR provider and the
agents' model use, so a single place serves them all. It reads the body of a
non-streaming JSON reply, puts the cost on the current span in the form Langfuse
ingests (verified against Langfuse v3's OpenTelemetry endpoint: `type=generation` is
what makes it a generation that carries a cost; on a plain span the cost is dropped),
hands the usage to whoever is collecting it (`collect_usage`) and the call to a
recorder. What it adds is numbers and ids only; the prompt and the answer, when they are
traced, are on the span pydantic-ai makes for the request (see `tracing`). Whatever goes
wrong in it is swallowed, because the request it observes must not fail for the sake of
bookkeeping.

A durable agent's model request runs in an activity that has no span of its own: the
request's span (`chat ...`) is made in workflow code. There the hook finds no span to
write on, so `model_cost` collects the usage around the request and carries the cost back
in the model's response, to be written on that span.
"""

import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx
from opentelemetry import trace
from temporalio import activity

from localization.observability.generation import COST_DETAILS as COST_DETAILS
from localization.observability.generation import MODEL_NAME as MODEL_NAME
from localization.observability.generation import OBSERVATION_TYPE as OBSERVATION_TYPE
from localization.observability.generation import USAGE_DETAILS as USAGE_DETAILS
from localization.observability.generation import LlmUsage, usage_attributes

log = logging.getLogger(__name__)

# A reply bigger than this is not a chat completion we want to read twice.
MAX_BODY_BYTES = 4 * 1024 * 1024
_WORKFLOW_ID = re.compile(r"video-([0-9]{1,32})-g([0-9]+)")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _count(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_usage(body: Any) -> LlmUsage | None:
    """The usage block of an OpenAI-compatible response body, or None when the body is
    not one. Every field is None when it is missing or not a sane number."""
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("cost_details")
    cost_details = (
        {
            k: v
            for k, raw in details.items()
            if (v := _number(raw)) is not None and isinstance(k, str)
        }
        if isinstance(details, dict)
        else {}
    )
    parsed = LlmUsage(
        generation_id=_text(body.get("id")),
        model=_text(body.get("model")),
        prompt_tokens=_count(usage.get("prompt_tokens")),
        completion_tokens=_count(usage.get("completion_tokens")),
        cost_usd=_number(usage.get("cost")),
        provider=_text(body.get("provider")),
        cost_details=cost_details,
    )
    if not usage and parsed.generation_id is None:
        return None
    return parsed


@dataclass(frozen=True)
class RunContext:
    aweme_id: str
    generation: int
    stage: str


def current_run() -> RunContext | None:
    """The video, generation and activity this request is made for, when it is made
    inside an activity of a localization workflow."""
    try:
        info = activity.info()
    except RuntimeError:
        return None
    match = _WORKFLOW_ID.fullmatch(info.workflow_id)
    if match is None:
        return None
    return RunContext(match[1], int(match[2]), info.activity_type)


Recorder = Callable[[LlmUsage, RunContext], Awaitable[None]]

_collected: ContextVar[list[LlmUsage] | None] = ContextVar("collected_llm_usage", default=None)


@contextmanager
def collect_usage() -> Iterator[list[LlmUsage]]:
    """The usage of every priced reply the hook reads inside the block, in order."""
    collected: list[LlmUsage] = []
    token = _collected.set(collected)
    try:
        yield collected
    finally:
        _collected.reset(token)


def make_response_hook(
    recorder: Recorder | None = None,
) -> Callable[[httpx.Response], Awaitable[None]]:
    async def hook(response: httpx.Response) -> None:
        try:
            usage = await _read(response)
            if usage is None:
                return
            collected = _collected.get()
            if collected is not None:
                collected.append(usage)
            span = trace.get_current_span()
            if span.is_recording():
                span.set_attributes(usage_attributes(usage))
            run = current_run()
            if recorder is not None and run is not None and usage.generation_id:
                await recorder(usage, run)
        except Exception:  # noqa: BLE001 - observing a request must never fail it
            log.warning("could not record the cost of a model request", exc_info=False)

    return hook


async def _read(response: httpx.Response) -> LlmUsage | None:
    if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
        return None  # an error, or a stream: the consumer reads those itself
    length = response.headers.get("content-length")
    if length is not None and int(length) > MAX_BODY_BYTES:
        return None
    # httpx reads the body into the response once; the consumer's own read after
    # the hooks returns this same content.
    await response.aread()
    try:
        return parse_usage(response.json())
    except ValueError:
        return None
