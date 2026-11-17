from typing import Annotated

from pydantic import Field, model_validator

from localization.contracts.base import Contract

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class BBox(Contract):
    """Pixel box, `[x, y, w, h]` in the source frame."""

    x: NonNegativeInt
    y: NonNegativeInt
    w: PositiveInt
    h: PositiveInt


class Watermark(Contract):
    """Text burned into the source that is not its subtitle and stays in one place: a
    channel name, a disclaimer in a corner. It is blurred over `region` from `start_ms`
    to `end_ms`. One that moves (a corner on the left, later one on the right) is one
    watermark per place."""

    region: BBox
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "Watermark":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be after start_ms")
        return self
