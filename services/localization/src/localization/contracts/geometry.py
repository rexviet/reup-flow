from typing import Annotated

from pydantic import Field

from localization.contracts.base import Contract

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class BBox(Contract):
    """Pixel box, `[x, y, w, h]` in the source frame."""

    x: NonNegativeInt
    y: NonNegativeInt
    w: PositiveInt
    h: PositiveInt
