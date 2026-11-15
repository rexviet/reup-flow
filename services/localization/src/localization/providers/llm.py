from typing import Literal

import httpx
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from localization.config import ConfigError, Settings
from localization.observability.llm_usage import Recorder, make_response_hook

Role = Literal["localizer", "dubber", "reviewer", "judge"]

# How long one model request may wait for the provider to answer. A reply is not streamed,
# so the first byte comes when the whole answer is done: this is the longest answer we wait
# for, and a request that outlasts it is a stalled or runaway one that is better asked
# again. Measured on this service's calls: ordinary answers took 3 to 70 s (up to 3.4k
# output tokens, about 50 tokens a second); one 4-line script produced 12.7k tokens of
# reasoning and took 228 s, and one request got no answer for 305 s. 180 s fits an answer of
# about 9k tokens (a long script) and is well below the 5 minute activity timeout, so a
# stalled attempt costs 3 minutes instead of the whole activity; the 4-line script's 228 s
# runaway fails and is asked again.
REQUEST_TIMEOUT_SECONDS = 180.0


def openrouter_client(
    recorder: Recorder | None = None, *, timeout: float = REQUEST_TIMEOUT_SECONDS
) -> httpx.AsyncClient:
    """The one HTTP client for OpenRouter, used by the agents' models and by OCR. Its
    response hook reads what each request cost (see `observability.llm_usage`)."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=5),
        event_hooks={"response": [make_response_hook(recorder)]},
    )


def model_for(
    role: Role,
    settings: Settings,
    http: httpx.AsyncClient | None = None,
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> OpenAIChatModel:
    """The chat model for an agent role, behind the configured OpenAI-compatible
    base URL (OpenRouter). The env value may carry pydantic-ai's `openai:` prefix."""
    name = getattr(settings, f"{role}_model")
    if not name and role == "dubber":
        # The Dubber shortens lines the Localizer wrote: until it has a model of its
        # own it uses the Localizer's, so an env file from before it existed starts.
        name = settings.localizer_model
    if not name:
        raise ConfigError(f"{role.upper()}_MODEL is not set")
    if settings.openrouter_api_key is None:
        raise ConfigError("OPENROUTER_API_KEY is not set")
    name = name.removeprefix("openai:")
    # The OpenAI client applies its own timeout to every request (not the HTTP client's) and
    # retries a failed one twice by itself. Both are set here: the timeout so a stalled
    # request fails when it should, and no silent retries, because Temporal retries the
    # activity and a second layer would multiply the wait.
    client = AsyncOpenAI(
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key.get_secret_value(),
        http_client=http,
        timeout=httpx.Timeout(timeout, connect=5),
        max_retries=0,
    )
    return OpenAIChatModel(name, provider=OpenAIProvider(openai_client=client))
