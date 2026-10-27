"""An S3 client on a directory, so a worker process that is killed and a test process
that watches it see the same bucket.

It models what the object store depends on: an object appears whole or not at all
(written aside, then renamed), user metadata names are folded to lower case with
hyphens as S3 does, and a missing key raises the same `ClientError` shape.
"""

import io
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError


def _missing(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, operation)


class FileS3:
    def __init__(self, root: Path) -> None:
        self._objects = root / "objects"
        self._meta = root / "meta"
        self._objects.mkdir(parents=True, exist_ok=True)
        self._meta.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._objects / key

    def _write(self, key: str, data: bytes, metadata: dict[str, str] | None) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".part-")
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        folded = {k.lower().replace("_", "-"): v for k, v in (metadata or {}).items()}
        side = self._meta / (key.replace("/", "__") + ".json")
        side.write_text(json.dumps(folded))
        os.replace(tmp, target)

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **extra: Any) -> dict:
        self._write(Key, bytes(Body), extra.get("Metadata"))
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        try:
            return {"Body": io.BytesIO(self._path(Key).read_bytes())}
        except FileNotFoundError:
            raise _missing("GetObject") from None

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if not self._path(Key).is_file():
            raise _missing("HeadObject")
        side = self._meta / (Key.replace("/", "__") + ".json")
        meta = json.loads(side.read_text()) if side.exists() else {}
        return {"Metadata": meta}

    def copy_object(self, *, Bucket: str, Key: str, CopySource: dict) -> dict:
        source = self._path(CopySource["Key"])
        if not source.is_file():
            raise _missing("CopyObject")
        self._write(
            Key,
            source.read_bytes(),
            self.head_object(Bucket=Bucket, Key=CopySource["Key"])["Metadata"],
        )
        return {}

    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict | None = None):
        self._write(key, Path(filename).read_bytes(), (ExtraArgs or {}).get("Metadata"))

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        try:
            shutil.copyfile(self._path(key), filename)
        except FileNotFoundError:
            raise _missing("GetObject") from None

    def keys(self, prefix: str = "") -> set[str]:
        return {
            str(p.relative_to(self._objects))
            for p in self._objects.rglob("*")
            if p.is_file() and not p.name.startswith(".part-")
            if str(p.relative_to(self._objects)).startswith(prefix)
        }
