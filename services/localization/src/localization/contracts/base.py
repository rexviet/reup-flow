from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """Immutable, closed data contract. Unknown fields are an error, not ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid")
