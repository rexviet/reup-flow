"""The cost OpenRouter reports for a durable agent's model request, on that request's span.

A durable agent's model request has two halves. In workflow code, pydantic-ai opens the
request's span (`chat <model>`, with the messages in and out and the token counts); the
request itself is sent from an activity, where the response hook reads what OpenRouter
says it cost. The activity has no span of its own, so the cost travels back with the
answer:

- `reported_cost.ReportedCostModel` (worker side, around the bound model) collects what
  the hook read for this request and puts the reported cost in the response
  (`with_reported_cost`: `usage.cost`, and the breakdown in `provider_details`);
- `CostOnRequestSpan` (an agent capability, workflow side, inside pydantic-ai's
  instrumentation) writes it on the `chat` span in the form Langfuse ingests.

So each model request is one observation carrying its tokens and its cost. Nothing here
prices a request: a response without a reported cost gets no cost. Workflow code imports
this module, so it does no I/O and reaches no HTTP client.
"""

from dataclasses import replace
from decimal import Decimal
from typing import Any

from opentelemetry import trace
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext

from localization.observability.generation import LlmUsage, usage_attributes

# Where the reported breakdown rides in `ModelResponse.provider_details`.
COST_DETAILS_KEY = "openrouter_cost_details"


def with_reported_cost(response: ModelResponse, reported: list[LlmUsage]) -> ModelResponse:
    """The response, with the cost of the reply it was parsed from when one was reported.
    The reply is matched by its generation id; a lone reply without one is taken as it."""
    usage = next(
        (
            u
            for u in reported
            if u.generation_id and u.generation_id == response.provider_response_id
        ),
        reported[0] if len(reported) == 1 and reported[0].generation_id is None else None,
    )
    if usage is None or usage.cost_usd is None:
        return response
    return replace(
        response,
        usage=replace(response.usage, cost=Decimal(str(usage.cost_usd))),
        provider_details={
            **(response.provider_details or {}),
            COST_DETAILS_KEY: usage.cost_details,
        },
    )


def request_usage(response: ModelResponse) -> LlmUsage:
    """What the span of this request says it used and cost, from the response alone."""
    details = (response.provider_details or {}).get(COST_DETAILS_KEY)
    return LlmUsage(
        generation_id=response.provider_response_id,
        model=response.model_name,
        prompt_tokens=response.usage.input_tokens or None,
        completion_tokens=response.usage.output_tokens or None,
        cost_usd=float(response.usage.cost) if response.usage.cost is not None else None,
        cost_details=dict(details) if isinstance(details, dict) else {},
    )


class CostOnRequestSpan(AbstractCapability[Any]):
    """Writes the reported cost and tokens on the current model request's span.

    pydantic-ai's instrumentation is the outermost capability, so while this one runs the
    current span is that request's `chat` span. It runs in workflow code: it only sets
    attributes, and while history is replayed the span is not exported anyway."""

    async def wrap_model_request(
        self, ctx: RunContext[Any], *, request_context: ModelRequestContext, handler: Any
    ) -> ModelResponse:
        response = await handler(request_context)
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attributes(usage_attributes(request_usage(response)))
        return response
