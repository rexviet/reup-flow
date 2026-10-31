"""A model request as Langfuse reads it: the attributes that make a span a generation
carrying the request's tokens and cost.

Pure (no I/O, no provider): workflow code writes these on a durable agent's request span
(see `model_cost`), and the response hook writes them on the span current in an
activity (see `llm_usage`).
"""

import json
from dataclasses import dataclass, field

# Langfuse's OpenTelemetry attribute names.
OBSERVATION_TYPE = "langfuse.observation.type"
MODEL_NAME = "langfuse.observation.model.name"
USAGE_DETAILS = "langfuse.observation.usage_details"
COST_DETAILS = "langfuse.observation.cost_details"


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
