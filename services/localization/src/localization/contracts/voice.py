import re
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, model_validator

from localization.contracts.base import Contract

_SECRET_FIELD_RE = re.compile(r"key|token|secret|password", re.IGNORECASE)

Provider = Literal["voicestudio", "openai_compat", "vbee", "zerotts"]


# The providers whose runs go through the VoiceStudio pipeline: speech is heard by VoiceStudio's
# speech-to-text and the result is delivered as a review project. Which one speaks the dub is
# the only difference between them.
REVIEW_PROVIDERS = frozenset({"voicestudio", "zerotts"})


class ProviderSettings(Contract):
    """Per-provider voice settings. Closed model: it travels through workflow
    history, so it must never be able to carry a credential. A field whose name
    looks like one is rejected when the class is defined."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        for name in cls.model_fields:
            if _SECRET_FIELD_RE.search(name):
                raise TypeError(
                    f"{cls.__name__}.{name}: provider settings may not carry credentials"
                )


class VoiceStudioSettings(ProviderSettings):
    speed: Annotated[float, Field(ge=0.25, le=4.0)] = 1.0
    instructions: Annotated[str, Field(max_length=500)] | None = None


class OpenAICompatSettings(ProviderSettings):
    speed: Annotated[float, Field(ge=0.25, le=4.0)] = 1.0


class ZeroTtsSettings(ProviderSettings):
    """ZeroTTS has no speed control: how fast a voice speaks is part of the voice."""

    @property
    def speed(self) -> float:
        """Always 1.0, so what reads a voice's speed (the measured speaking rate is divided
        by it) needs no case for a provider without one. Not a field: it cannot be set."""
        return 1.0


class VbeeSettings(ProviderSettings):
    # The range Vbee's TTS accepts.
    speed: Annotated[float, Field(ge=0.25, le=1.9)] = 1.0


# One entry per provider. Adding a provider is one line here plus its settings
# class; no existing provider's contract changes.
SETTINGS_BY_PROVIDER: dict[str, type[ProviderSettings]] = {
    "voicestudio": VoiceStudioSettings,
    "openai_compat": OpenAICompatSettings,
    "vbee": VbeeSettings,
    "zerotts": ZeroTtsSettings,
}


class VoiceRef(Contract):
    provider: Provider
    voice_id: Annotated[str, Field(min_length=1, max_length=128)]
    settings: VoiceStudioSettings | OpenAICompatSettings | VbeeSettings | ZeroTtsSettings

    @model_validator(mode="before")
    @classmethod
    def _settings_follow_the_provider(cls, data: Any) -> Any:
        """Discriminates on `provider`: the settings of a voice are parsed as the
        class registered for its provider, never guessed from their fields."""
        if isinstance(data, dict) and isinstance(data.get("settings"), dict):
            settings_cls = SETTINGS_BY_PROVIDER.get(data.get("provider"))  # type: ignore[arg-type]
            if settings_cls is not None:
                data = {**data, "settings": settings_cls.model_validate(data["settings"])}
        return data

    @model_validator(mode="after")
    def _settings_match_the_provider(self) -> "VoiceRef":
        if type(self.settings) is not SETTINGS_BY_PROVIDER[self.provider]:
            raise ValueError(f"settings of the wrong type for provider {self.provider}")
        return self
