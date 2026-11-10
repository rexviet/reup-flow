"""Closed browser commands and durable worker request references."""

from typing import Annotated

from pydantic import Field, model_validator

from localization.contracts.base import Contract
from localization.contracts.review_project import RevisionId

RequestId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{16,64}$")]


class SaveCue(Contract):
    expectedRevision: RevisionId
    headETag: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(max_length=4000)]
    start_ms: Annotated[int, Field(strict=True, ge=0)]
    end_ms: Annotated[int, Field(strict=True, gt=0)]

    @model_validator(mode="after")
    def timing(self):
        if self.start_ms >= self.end_ms:
            raise ValueError("start must precede end")
        return self


class RevoiceCue(Contract):
    requestId: RequestId
    cueId: Annotated[int, Field(strict=True, ge=0)]
    baseRevision: RevisionId


def mismatch(cue) -> bool:
    return (
        cue.text_vi != cue.spoken_text
        or bool(cue.text_vi.strip()) != (cue.clip is not None)
        or (
            cue.clip is not None
            and (
                cue.start_ms != cue.audio_start_ms
                or cue.audio_start_ms + cue.audio_duration_ms / cue.stretch_ratio > cue.end_ms + 1
            )
        )
    )
