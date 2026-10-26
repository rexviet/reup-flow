import re
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from localization.contracts.base import Contract

_SECRET_FIELD_RE = re.compile(r"key|token|secret|password", re.IGNORECASE)


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


class VoiceRef(Contract):
    provider: Literal["voicestudio"]
    voice_id: Annotated[str, Field(min_length=1, max_length=128)]
    settings: VoiceStudioSettings
