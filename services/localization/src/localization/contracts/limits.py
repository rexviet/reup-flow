"""The Temporal payload limit, and a check workflow code can run before it hands
data to an activity or returns it.

Why a pre-check exists: `SizeGuardCodec` raises while the workflow's own commands
are being encoded. Inside a workflow that surfaces as a failed workflow TASK, and
Temporal retries a failed task forever, so the workflow stays RUNNING instead of
failing. Raising a non-retryable `ApplicationError` from workflow code fails the
workflow cleanly, which is what a too-large payload deserves.
"""

import json
from typing import Any

from pydantic import BaseModel
from temporalio.exceptions import ApplicationError

MAX_PAYLOAD_BYTES = 256 * 1024


def payload_size(value: Any) -> int:
    """Encoded size of `value` as it would travel: JSON for models and plain data."""
    if isinstance(value, bytes | bytearray):
        return len(value)
    if isinstance(value, str):
        return len(value.encode()) + 2
    if isinstance(value, BaseModel):
        return len(value.model_dump_json().encode())
    return len(json.dumps(value, default=str).encode())


def ensure_payload_fits(value: Any, what: str, limit: int = MAX_PAYLOAD_BYTES) -> None:
    size = payload_size(value)
    if size > limit:
        raise ApplicationError(
            f"{what} is {size} bytes, over the {limit} byte payload limit; "
            "store it in the object store and pass the key",
            type="PayloadTooLarge",
            non_retryable=True,
        )
