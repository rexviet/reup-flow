"""Identifier grammar. Every id that ends up in an object key, a workflow id or a
filesystem path goes through these validators first."""

import re
from typing import Annotated

from pydantic import AfterValidator, Field

# `[0-9]`, never `\d`: in Python and in pydantic's regex engine `\d` also matches
# Arabic-Indic and other Unicode digits. `fullmatch`, never `match` with `$`: `$`
# accepts a trailing newline.
AWEME_ID_RE = re.compile(r"[0-9]{1,32}")
CHANNEL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
KEY_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
# A storage key is `/`-separated segments of the name charset; no empty segment,
# no `.`/`..` segment, no leading slash, no backslash.
STORAGE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._=-]*(/[A-Za-z0-9][A-Za-z0-9._=-]*)*")
STORAGE_KEY_MAX = 1024
SOURCE_PREFIX = "source/"


def validate_aweme_id(value: str) -> str:
    if not isinstance(value, str) or AWEME_ID_RE.fullmatch(value) is None:
        raise ValueError("aweme_id must be 1-32 ASCII digits")
    return value


def validate_channel_id(value: str) -> str:
    if not isinstance(value, str) or CHANNEL_ID_RE.fullmatch(value) is None:
        raise ValueError("channel_id must be 1-128 characters of [A-Za-z0-9_-]")
    return value


def validate_key_name(value: str) -> str:
    # Rejects separators and a leading dot, so a name can neither add a path
    # segment nor be `.`/`..`.
    if not isinstance(value, str) or KEY_NAME_RE.fullmatch(value) is None or ".." in value:
        raise ValueError("name must be a single object-key segment")
    return value


def validate_sha256(value: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError("expected a lowercase hex sha256")
    return value


def validate_storage_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > STORAGE_KEY_MAX
        or STORAGE_KEY_RE.fullmatch(value) is None
        or any(part in (".", "..") or part.startswith("..") for part in value.split("/"))
    ):
        raise ValueError("not a valid storage key")
    return value


def validate_source_key(value: str) -> str:
    validate_storage_key(value)
    if not value.startswith(SOURCE_PREFIX):
        raise ValueError(f"a source video key must start with {SOURCE_PREFIX}")
    return value


def validate_non_negative(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what} must be a non-negative integer")
    return value


AwemeId = Annotated[str, AfterValidator(validate_aweme_id)]
ChannelId = Annotated[str, AfterValidator(validate_channel_id)]
# The pattern is also what lands in the JSON Schema the renderer validates with.
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$"), AfterValidator(validate_sha256)]
StorageKey = Annotated[
    str, Field(min_length=1, max_length=STORAGE_KEY_MAX), AfterValidator(validate_storage_key)
]
SourceKey = Annotated[
    str, Field(min_length=1, max_length=STORAGE_KEY_MAX), AfterValidator(validate_source_key)
]
