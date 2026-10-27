from typing import Literal

import httpx
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from localization.config import ConfigError, Settings
from localization.observability.llm_usage import Recorder, make_response_hook

Role = Literal["localizer", "dubber", "reviewer", "judge"]


def openrouter_client(recorder: Recorder | None = None) -> httpx.AsyncClient:
    """The one HTTP client for OpenRouter, used by the agents' models and by OCR. Its
    response hook reads what each request cost (see `observability.llm_usage`)."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(600, connect=5),
        event_hooks={"response": [make_response_hook(recorder)]},
    )


def model_for(
    role: Role, settings: Settings, http: httpx.AsyncClient | None = None
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
    provider = OpenAIProvider(
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key.get_secret_value(),
        http_client=http,
    )
    return OpenAIChatModel(name, provider=provider)
