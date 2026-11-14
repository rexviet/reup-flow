"""S3 object store. The key functions below are the only place a key is built."""

import hashlib
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

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


def work_key_for(spec, round: int, name: str) -> str:
    if spec.source_snapshot is None:
        return work_key(spec.aweme_id, round, name)
    return (
        f"work/{validate_aweme_id(spec.aweme_id)}/"
        f"g{validate_non_negative(spec.generation, 'generation')}/"
        f"r{validate_non_negative(round, 'round')}/{validate_key_name(name)}"
    )


def require_work_key(spec, key: str) -> None:
    validate_storage_key(key)
    if spec.source_snapshot is None:
        return
    prefix = f"work/{spec.aweme_id}/g{spec.generation}/"
    if not key.startswith(prefix):
        raise ValueError("artifact belongs to another generation")


def source_video_key(spec) -> str:
    if spec.source_snapshot is None:
        return spec.video_key
    require_work_key(spec, spec.source_snapshot.snapshot_key)
    return spec.source_snapshot.snapshot_key


def project_head_key(aweme_id: str, generation: int) -> str:
    return (
        f"review/{validate_aweme_id(aweme_id)}/"
        f"g{validate_non_negative(generation, 'generation')}/head.json"
    )


class StorageConflictError(Exception):
    """A conditional write lost; no published bytes were changed."""


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


def _is_missing_on_head(exc: ClientError) -> bool:
    """A HEAD on a key that is not there. A caller without an unconditioned
    `s3:ListBucket` (this service's policy allows listing only under its prefixes) is told
    403 rather than 404 by AWS S3 and some compatible stores, which cannot be told apart
    from a real refusal. The key space is confined by the policy and a real refusal fails
    again on the write that follows, so a 403 here means "not there"."""
    code = exc.response.get("Error", {}).get("Code")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return _is_missing(exc) or code in ("403", "Forbidden", "AccessDenied") or status == 403


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

    def download_fileobj(self, key: str, out: BinaryIO, *, produced_by: str) -> None:
        """Streams the object into a file the caller already opened, for a caller that must
        control how the file is created (`download_file` creates its own temporary name)."""
        validate_storage_key(key)
        try:
            self._client.download_fileobj(self._bucket, key, out)
        except ClientError as exc:
            if _is_missing(exc):
                raise ArtifactMissingError(key, produced_by) from None
            raise

    def copy(self, src_key: str, dst_key: str, *, produced_by: str) -> None:
        """Copies an object inside the bucket without its bytes passing through this
        process. A missing source is reported like any missing artifact."""
        validate_storage_key(src_key)
        validate_storage_key(dst_key)
        try:
            self._client.copy_object(
                Bucket=self._bucket,
                Key=dst_key,
                CopySource={"Bucket": self._bucket, "Key": src_key},
            )
        except ClientError as exc:
            if _is_missing(exc):
                raise ArtifactMissingError(src_key, produced_by) from None
            raise

    def metadata(self, key: str) -> dict[str, str] | None:
        """The user metadata of an object, or None when there is no such object."""
        validate_storage_key(key)
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_missing_on_head(exc):
                return None
            raise
        return dict(head.get("Metadata") or {})

    def exists(self, key: str) -> bool:
        validate_storage_key(key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if _is_missing_on_head(exc):
                return False
            raise

    def read_version(self, key: str) -> tuple[bytes, str]:
        """Body and ETag from one GET, never two independently racing reads."""
        validate_storage_key(key)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_missing(exc):
                raise ArtifactMissingError(key, "review_project") from None
            raise
        with response["Body"] as body:
            return body.read(), response["ETag"]

    def compare_and_swap(self, key: str, data: bytes, etag: str | None) -> str:
        validate_storage_key(key)
        condition = {"IfMatch": etag} if etag is not None else {"IfNoneMatch": "*"}
        try:
            result = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType="application/json",
                **condition,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in (
                "409",
                "412",
                "PreconditionFailed",
                "ConditionalRequestConflict",
            ):
                raise StorageConflictError(key) from None
            raise
        return result["ETag"]

    def capture_source(self, source: str, destination: str):
        """Pin a source version before preparation. Conditional GET detects replacement."""
        from localization.contracts.review_project import SourceSnapshot

        validate_storage_key(source)
        validate_storage_key(destination)
        identity_key = destination + ".json"
        try:
            stored, _ = self.read_version(identity_key)
        except ArtifactMissingError:
            pass
        else:
            snapshot = SourceSnapshot.model_validate_json(stored)
            if snapshot.source_key != source or snapshot.snapshot_key != destination:
                raise ValueError("source snapshot identity mismatch")
            self.verify_file(destination, snapshot.sha256)
            return snapshot
        head = self._client.head_object(Bucket=self._bucket, Key=source)
        version = head.get("VersionId")
        condition = {"IfMatch": head["ETag"]}
        if version is not None:
            condition["VersionId"] = version
        response = self._client.get_object(Bucket=self._bucket, Key=source, **condition)
        with tempfile.TemporaryDirectory(prefix="snapshot-") as tmp:
            path = Path(tmp) / "source.mp4"
            with response["Body"] as body, path.open("wb") as out:
                while block := body.read(8 * 1024 * 1024):
                    out.write(block)
            digest = self.put_immutable_file(destination, path, content_type="video/mp4")
        snapshot = SourceSnapshot(
            source_key=source,
            etag=head["ETag"],
            version_id=version,
            snapshot_key=destination,
            sha256=digest,
        )
        self.put_immutable_bytes(identity_key, snapshot.model_dump_json().encode())
        return snapshot

    def verify_file(self, key: str, digest: str) -> None:
        validate_storage_key(key)
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        check = hashlib.sha256()
        with response["Body"] as body:
            while block := body.read(8 * 1024 * 1024):
                check.update(block)
        if check.hexdigest() != digest:
            raise ValueError(f"immutable checksum mismatch: {key}")

    def put_immutable_bytes(self, key: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        try:
            self.compare_and_swap(key, data, None)
        except StorageConflictError:
            self.verify_file(key, digest)
        return digest

    def put_immutable_file(
        self, key: str, path: Path, *, content_type: str = "application/octet-stream"
    ) -> str:
        """Conditional PUT or conditional multipart completion; never destination COPY."""
        validate_storage_key(key)
        with path.open("rb") as src:
            digest = hashlib.file_digest(src, "sha256").hexdigest()
        upload_id = None
        try:
            size = path.stat().st_size
            if size <= 8 * 1024 * 1024:
                with path.open("rb") as src:
                    self._client.put_object(
                        Bucket=self._bucket,
                        Key=key,
                        Body=src,
                        IfNoneMatch="*",
                        ContentType=content_type,
                    )
            else:
                upload = self._client.create_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    ContentType=content_type,
                )
                upload_id = upload["UploadId"]
                parts = []
                chunk_size = max(8 * 1024 * 1024, (size + 9998) // 9999)
                with path.open("rb") as src:
                    while block := src.read(chunk_size):
                        number = len(parts) + 1
                        part = self._client.upload_part(
                            Bucket=self._bucket,
                            Key=key,
                            UploadId=upload_id,
                            PartNumber=number,
                            Body=block,
                        )
                        parts.append({"ETag": part["ETag"], "PartNumber": number})
                self._client.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    IfNoneMatch="*",
                )
                upload_id = None
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("412", "PreconditionFailed"):
                self.verify_file(key, digest)
            else:
                raise
        finally:
            if upload_id is not None:
                self._client.abort_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                )
        return digest
