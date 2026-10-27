"""What a model request cost, as the provider reports it.

OpenRouter puts `usage.cost` (USD) and `usage.cost_details` in every chat completion,
and the generation id (`gen-...`) at the top of the body. Nothing here estimates a
price: a response without a cost has no cost.

`make_response_hook` is attached to the one HTTP client both the OCR provider and the
agents' model use, so a single place serves them all. It reads the body of a
non-streaming JSON reply, puts the cost on the current span in the form Langfuse
ingests (verified against Langfuse v3's OpenTelemetry endpoint: `type=generation` is
what makes it a generation that carries a cost; on a plain span the cost is dropped),
and hands the call to a recorder. It records only numbers and ids: never a prompt, an
image, a header or a key. Whatever goes wrong in it is swallowed, because the request
it observes must not fail for the sake of bookkeeping.
"""

import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from opentelemetry import trace
from temporalio import activity

log = logging.getLogger(__name__)

# Langfuse's OpenTelemetry attribute names.
OBSERVATION_TYPE = "langfuse.observation.type"
MODEL_NAME = "langfuse.observation.model.name"
USAGE_DETAILS = "langfuse.observation.usage_details"
COST_DETAILS = "langfuse.observation.cost_details"

# A reply bigger than this is not a chat completion we want to read twice.
MAX_BODY_BYTES = 4 * 1024 * 1024
_WORKFLOW_ID = re.compile(r"video-([0-9]{1,32})-g([0-9]+)")


@dataclass(frozen=True)
class LlmUsage:
    generation_id: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    # The OpenRouter provider that served the request (the body's `provider`).
    provider: str | None = None
    cost_details: dict[str, float] = field(default_factory=dict)


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


def usage_attributes(usage: LlmUsage) -> dict[str, str]:
    """Span attributes for Langfuse. Only what is known is set."""
    attributes = {OBSERVATION_TYPE: "generation"}
    if usage.model:
        attributes[MODEL_NAME] = usage.model
    tokens = {
        "input": usage.prompt_tokens,
        "output": usage.completion_tokens,
    }
    tokens = {k: v for k, v in tokens.items() if v is not None}
    if tokens:
        tokens["total"] = sum(tokens.values())
        attributes[USAGE_DETAILS] = json.dumps(tokens)
    if usage.cost_usd is not None:
        attributes[COST_DETAILS] = json.dumps({**usage.cost_details, "total": usage.cost_usd})
    return attributes


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


def make_response_hook(
    recorder: Recorder | None = None,
) -> Callable[[httpx.Response], Awaitable[None]]:
    async def hook(response: httpx.Response) -> None:
        try:
            usage = await _read(response)
            if usage is None:
                return
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
