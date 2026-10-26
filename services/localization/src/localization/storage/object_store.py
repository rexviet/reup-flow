"""S3 object store. The key functions below are the only place a key is built."""

from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from temporalio.exceptions import ApplicationError

from localization.config import ConfigError, Settings
from localization.contracts.ids import (
    validate_aweme_id,
    validate_key_name,
    validate_non_negative,
    validate_sha256,
    validate_storage_key,
)

# --- keys -------------------------------------------------------------------


def work_key(aweme_id: str, round: int, name: str) -> str:
    return (
        f"work/{validate_aweme_id(aweme_id)}/r{validate_non_negative(round, 'round')}/"
        f"{validate_key_name(name)}"
    )


def tts_cache_key(hash: str) -> str:
    return f"cache/tts/{validate_sha256(hash)}.wav"


def render_key(aweme_id: str, spec_sha: str) -> str:
    return f"work/{validate_aweme_id(aweme_id)}/render-{validate_sha256(spec_sha)}.mp4"


def review_key(aweme_id: str, generation: int) -> str:
    aweme_id = validate_aweme_id(aweme_id)
    return f"review/{aweme_id}-g{validate_non_negative(generation, 'generation')}.mp4"


def output_key(aweme_id: str) -> str:
    return f"output/{validate_aweme_id(aweme_id)}.mp4"


# --- errors -----------------------------------------------------------------


class ArtifactMissingError(ApplicationError):
    """An artifact a stage depends on is gone. Retrying the reader cannot bring it
    back, so it is non-retryable; `stage` names the stage that produced it, which
    is the one the workflow re-runs (once)."""

    def __init__(self, key: str, stage: str) -> None:
        super().__init__(
            f"artifact {key} is missing (produced by stage {stage})",
            stage,
            non_retryable=True,
            type="ArtifactMissingError",
        )
        self.key = key
        self.stage = stage


_MISSING_CODES = {"NoSuchKey", "404", "NotFound"}


def _is_missing(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in _MISSING_CODES


# --- store ------------------------------------------------------------------


class ObjectStore:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_settings(cls, settings: Settings) -> "ObjectStore":
        if settings.loc_s3_access_key_id is None or settings.loc_s3_secret_access_key is None:
            raise ConfigError("LOC_S3_ACCESS_KEY_ID / LOC_S3_SECRET_ACCESS_KEY are not set")
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.loc_s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.loc_s3_secret_access_key.get_secret_value(),
            config=Config(
                s3={"addressing_style": "path" if settings.s3_force_path_style else "auto"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(client, settings.s3_bucket)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        validate_storage_key(key)
        extra: dict[str, Any] = {"ContentType": content_type} if content_type else {}
        if metadata:
            extra["Metadata"] = metadata
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, **extra)

    def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> None:
        validate_storage_key(key)
        extra = {"ContentType": content_type} if content_type else {}
        self._client.upload_file(str(path), self._bucket, key, ExtraArgs=extra or None)

    def get_bytes(self, key: str, *, produced_by: str) -> bytes:
        validate_storage_key(key)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()
        except ClientError as exc:
            if _is_missing(exc):
                raise ArtifactMissingError(key, produced_by) from None
            raise

    def download_file(self, key: str, path: Path, *, produced_by: str) -> None:
        validate_storage_key(key)
        try:
            self._client.download_file(self._bucket, key, str(path))
        except ClientError as exc:
            if _is_missing(exc):
                raise ArtifactMissingError(key, produced_by) from None
            raise

    def metadata(self, key: str) -> dict[str, str] | None:
        """The user metadata of an object, or None when there is no such object."""
        validate_storage_key(key)
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_missing(exc):
                return None
            raise
        return dict(head.get("Metadata") or {})

    def exists(self, key: str) -> bool:
        validate_storage_key(key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if _is_missing(exc):
                return False
            raise
