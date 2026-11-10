"""Immutable revisions, HEAD CAS and recoverable local Studio projections."""

import contextlib
import fcntl
import json
import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from localization.activities.review_project import checksum, project_html, resolve_project
from localization.activities.srt_document import srt_document
from localization.contracts import ReviewAsset, ReviewHead, ReviewProjectManifest
from localization.storage.object_store import (
    ArtifactMissingError,
    StorageConflictError,
    project_head_key,
)


class Conflict(Exception):
    def __init__(self, revision: str, message="Project changed; reload before retrying"):
        super().__init__(message)
        self.revision = revision


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def safe_path(root: Path, name: str) -> Path:
    path = root / name
    if root.is_symlink() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError("symlink in project path")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("outside project")
    return path


class Repository:
    def __init__(self, store, root: Path | None = None):
        self.store = store
        self.root = root

    def current(self, aweme, generation):
        return resolve_project(self.store, project_head_key(aweme, generation))

    def asset(self, project, data: bytes, suffix: str):
        digest = checksum(data)
        key = f"review/{project.aweme_id}/g{project.generation}/assets/{digest}{suffix}"
        self.store.put_immutable_bytes(key, data)
        return ReviewAsset(key=key, sha256=digest)

    def read_asset(self, asset):
        data = self.store.get_bytes(asset.key, produced_by="review_project")
        if checksum(data) != asset.sha256:
            raise ValueError("asset checksum mismatch")
        return data

    def manifest(self, aweme, generation, revision):
        key = f"review/{aweme}/g{generation}/r{revision}/manifest.json"
        return ReviewProjectManifest.model_validate_json(
            self.store.get_bytes(key, produced_by="review_project")
        )

    def committed(self, candidate) -> bool:
        _, current, _ = self.current(candidate.aweme_id, candidate.generation)
        seen = set()
        while current.revision_id not in seen:
            if current.revision_id == candidate.revision_id:
                return True
            seen.add(current.revision_id)
            if current.parent_revision_id is None:
                return False
            current = self.manifest(
                current.aweme_id, current.generation, current.parent_revision_id
            )
        raise ValueError("cyclic revision ancestry")

    def prepare(
        self, base, cues, visual, *, revision=None, operation=None, mixed=None, studio_files=None
    ):
        # Source masks and playback script stay generated, while visual edits use cue IDs.
        from localization.review_bridge.visual import apply_visual

        mixed = mixed or base.mixed_audio
        subtitles = SimpleNamespace(
            region=base.source_region,
            cues=[SimpleNamespace(start_ms=s, end_ms=e) for s, e in base.source_cues],
        )
        with tempfile.TemporaryDirectory(prefix="review-caption-font-") as temp:
            font_path = Path(temp) / "caption.ttf"
            font_path.write_bytes(self.read_asset(base.font))
            source = apply_visual(
                project_html(
                    cues, base.video, mixed, base.font, subtitles, base, font_path=font_path
                ), visual
            )
        candidate = base.model_copy(
            update={
                "revision_id": revision or uuid.uuid4().hex,
                "parent_revision_id": base.revision_id,
                "operation_id": operation,
                "cues": tuple(cues),
                "mixed_audio": mixed,
                "html": self.asset(base, source, ".html"),
                "srt": self.asset(base, srt_document(cues), ".srt"),
                "studio_files": base.studio_files if studio_files is None else studio_files,
            }
        )
        candidate = ReviewProjectManifest.model_validate(candidate.model_dump())
        raw = candidate.model_dump_json().encode()
        self.store.put_immutable_bytes(
            f"review/{base.aweme_id}/g{base.generation}/r{candidate.revision_id}/manifest.json", raw
        )
        return candidate

    def activate(self, candidate, etag):
        if self.committed(candidate):
            return candidate.revision_id
        raw = candidate.model_dump_json().encode()
        head = ReviewHead(
            aweme_id=candidate.aweme_id,
            generation=candidate.generation,
            revision_id=candidate.revision_id,
            source=candidate.source,
            manifest_key=(
                f"review/{candidate.aweme_id}/g{candidate.generation}/"
                f"r{candidate.revision_id}/manifest.json"
            ),
            manifest_sha256=checksum(raw),
        )
        try:
            self.store.compare_and_swap(
                project_head_key(candidate.aweme_id, candidate.generation),
                head.model_dump_json().encode(),
                etag,
            )
        except StorageConflictError:
            if not self.committed(candidate):
                raise Conflict(
                    self.current(candidate.aweme_id, candidate.generation)[0].revision_id
                ) from None
        return candidate.revision_id

    def folder(self, aweme, generation):
        # project_head_key performs the identifier validation before any filesystem access.
        project_head_key(aweme, generation)
        if self.root is None:
            raise ValueError("local projection is unavailable")
        folder = safe_path(self.root, f"{aweme}/g{generation}")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @contextlib.contextmanager
    def lock(self, aweme, generation):
        folder = self.folder(aweme, generation)
        path = safe_path(folder, ".companion.lock")
        with path.open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield folder

    def projection(self, folder):
        files = {}
        for path in folder.rglob("*"):
            if path.is_symlink():
                raise ValueError("symlink in project projection")
            name = path.relative_to(folder).as_posix()
            if (
                path.is_file()
                and path.suffix in {".html", ".json", ".css"}
                and not name.startswith(
                    (".hyperframes/preview/", ".thumbnails/", ".waveform-cache/")
                )
                and name != ".hyperframes/hf-ids-stamped.json"
                and name
                not in {
                    "manifest.json",
                    "head.json",
                    ".projection.json",
                    ".projection-pending.json",
                }
                and not (len(path.stem) == 64 and all(c in "0123456789abcdef" for c in path.stem))
            ):
                if path.stat().st_size > 2_000_000:
                    raise ValueError("Studio file too large")
                files[name] = path.read_bytes()
        return files

    def fingerprint(self, files):
        return checksum(json.dumps({k: checksum(v) for k, v in sorted(files.items())}).encode())

    def materialize(self, aweme, generation, *, allow_dirty=False):
        with self.lock(aweme, generation) as folder:
            _, project, etag = self.current(aweme, generation)
            before = self.projection(folder)
            marker = safe_path(folder, ".projection.json")
            pending_path = safe_path(folder, ".projection-pending.json")
            if pending_path.exists():
                pending = json.loads(pending_path.read_bytes())
                hashes = {k: checksum(v) for k, v in before.items()}
                names = set(hashes) | set(pending["before"]) | set(pending["after"])
                if any(
                    hashes.get(k) not in {pending["before"].get(k), pending["after"].get(k)}
                    for k in names
                ):
                    raise Conflict(project.revision_id, "Studio changed during interrupted refresh")
            elif marker.exists():
                state = json.loads(marker.read_bytes())
                if self.fingerprint(before) != state["fingerprint"]:
                    if allow_dirty:
                        return project, etag
                    raise Conflict(
                        project.revision_id, "Save Studio edits before refreshing projection"
                    )
            elif before:
                raise Conflict(
                    project.revision_id, "Untracked Studio source; refusing to overwrite"
                )
            assets = (
                [
                    getattr(project, n)
                    for n in (
                        "video",
                        "mixed_audio",
                        "bgm",
                        "font",
                        "script",
                        "dub",
                        "subtitles",
                        "html",
                        "srt",
                    )
                ]
                + [c.clip for c in project.cues if c.clip]
                + list(project.studio_files.values())
            )
            # Read dependencies before mutating the projection, then check for a Studio race.
            staged = {Path(a.key).name: self.read_asset(a) for a in assets}
            staged.update({name: self.read_asset(a) for name, a in project.studio_files.items()})
            staged.update(
                {
                    "index.html": self.read_asset(project.html),
                    "captions.srt": self.read_asset(project.srt),
                    "manifest.json": project.model_dump_json().encode(),
                }
            )
            if self.projection(folder) != before:
                raise Conflict(project.revision_id, "Studio changed during materialization")
            after = {
                name: data
                for name, data in staged.items()
                if name == "index.html" or name in project.studio_files
            }
            pending = {
                "before": {name: checksum(data) for name, data in before.items()},
                "after": {name: checksum(data) for name, data in after.items()},
            }
            atomic_write(pending_path, json.dumps(pending).encode())
            for name, data in staged.items():
                if name in before or name in after:
                    path = safe_path(folder, name)
                    actual = path.read_bytes() if path.exists() else None
                    if actual != before.get(name):
                        raise Conflict(
                            project.revision_id,
                            "Studio changed before projection write",
                        )
                atomic_write(safe_path(folder, name), data)
            if self.projection(folder) != after:
                raise Conflict(project.revision_id, "Studio changed after projection write")
            atomic_write(
                marker,
                json.dumps(
                    {
                        "revision": project.revision_id,
                        "fingerprint": self.fingerprint(self.projection(folder)),
                    }
                ).encode(),
            )
            pending_path.unlink()
            return project, etag

    def request_key(self, aweme, generation, request_id):
        from localization.review_bridge.models import RevoiceCue

        # Reuse closed command grammar even for CLI/activity callers.
        RevoiceCue(requestId=request_id, cueId=0, baseRevision="0" * 32)
        project_head_key(aweme, generation)
        return f"review/{aweme}/g{generation}/requests/{request_id}.json"

    def read_json(self, key):
        return json.loads(self.store.get_bytes(key, produced_by="review_companion"))

    def optional_json(self, key):
        try:
            return self.read_json(key)
        except ArtifactMissingError as exc:
            if exc.key != key:
                raise
            return None
