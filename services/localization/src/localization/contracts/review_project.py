"""Durable review references. All dependencies belong to one generation."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from localization.contracts.base import Contract
from localization.contracts.collection import HexColor, MusicOffsetDb, OpacityPct
from localization.contracts.geometry import BBox, NonNegativeInt, PositiveInt
from localization.contracts.ids import AwemeId, Sha256, SourceKey, StorageKey
from localization.contracts.voice import VoiceRef

RevisionId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class SourceSnapshot(Contract):
    source_key: SourceKey
    etag: str
    version_id: str | None = None
    snapshot_key: StorageKey
    sha256: Sha256


class ReviewAsset(Contract):
    key: StorageKey
    sha256: Sha256


class ReviewCue(Contract):
    id: NonNegativeInt
    source_segment_ids: tuple[NonNegativeInt, ...]
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    text_vi: Annotated[str, Field(max_length=4000)]
    status: Literal["translated", "inferred", "unresolved"]
    reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    clip: ReviewAsset | None = None
    spoken_text: str
    audio_start_ms: NonNegativeInt
    audio_duration_ms: NonNegativeInt
    stretch_ratio: Annotated[float, Field(gt=0)]
    overflow_unresolved: bool

    @model_validator(mode="after")
    def _timing(self) -> "ReviewCue":
        if self.end_ms < self.start_ms:
            raise ValueError("cue ends before it starts")
        if not self.source_segment_ids:
            raise ValueError("cue needs source segment IDs")
        if self.status == "unresolved" and self.text_vi != "":
            raise ValueError("unresolved cue must be empty")
        if self.status != "translated" and (self.reason is None or not self.reason.strip()):
            raise ValueError("inferred/unresolved cue needs a reason")
        return self


class ReviewProjectManifest(Contract):
    schema_version: Literal[1] = 1
    aweme_id: AwemeId
    generation: NonNegativeInt
    revision_id: RevisionId
    parent_revision_id: RevisionId | None = None
    operation_id: Annotated[str, Field(max_length=64)] | None = None
    studio_files: dict[str, ReviewAsset] = Field(default_factory=dict)
    source: SourceSnapshot
    script_version: NonNegativeInt
    voice: VoiceRef
    model: Annotated[str, Field(min_length=1)]
    engine: str | None
    duration_ms: PositiveInt
    width: PositiveInt
    height: PositiveInt
    fps: PositiveInt
    video: ReviewAsset
    mixed_audio: ReviewAsset
    bgm: ReviewAsset
    font: ReviewAsset
    script: ReviewAsset
    dub: ReviewAsset
    subtitles: ReviewAsset
    html: ReviewAsset
    srt: ReviewAsset
    cues: tuple[ReviewCue, ...]
    source_region: BBox | None
    source_cues: tuple[tuple[NonNegativeInt, NonNegativeInt], ...]
    # The run's collection settings, so every revision (a Studio save, a revoice) is
    # drawn and mixed as the first was. A project written before them has none: its
    # captions are yellow and its source mask the white blur it always had.
    caption_color: HexColor | None = None
    # Both set or both unset: a project resolves the worker's defaults when it is built.
    cover_color: HexColor | None = None
    cover_opacity_pct: OpacityPct | None = None
    bgm_offset_db: MusicOffsetDb | None = None

    @model_validator(mode="after")
    def _closed_dependencies(self) -> "ReviewProjectManifest":
        prefix = f"review/{self.aweme_id}/g{self.generation}/assets/"
        assets = (
            [
                getattr(self, n)
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
            + [c.clip for c in self.cues if c.clip is not None]
            + list(self.studio_files.values())
        )
        # Workflow contracts cannot import filesystem helpers.
        # Canonical names prevent aliases from bypassing the reserved names.
        for name in self.studio_files:
            parts = name.split("/")
            stem, _, extension = parts[-1].rpartition(".")
            if (
                any(part in {"", ".", ".."} for part in parts)
                or "\\" in name
                or name
                in {
                    "index.html",
                    "manifest.json",
                    "head.json",
                    "captions.srt",
                    ".projection.json",
                    ".projection-pending.json",
                }
                or not stem
                or extension not in {"json", "css", "html"}
            ):
                raise ValueError("invalid Studio projection file")
        for asset in assets:
            if (
                not asset.key.startswith(prefix + asset.sha256 + ".")
                or "/" in asset.key[len(prefix) :]
            ):
                raise ValueError("dependency must be content-addressed in this generation")
        if self.source.snapshot_key != f"work/{self.aweme_id}/g{self.generation}/r0/source.mp4":
            raise ValueError("source snapshot belongs to another generation")
        if self.source.sha256 != self.video.sha256:
            raise ValueError("video differs from captured source")
        if len({c.id for c in self.cues}) != len(self.cues):
            raise ValueError("duplicate cue ID")
        if (self.cover_color is None) != (self.cover_opacity_pct is None):
            raise ValueError("cover colour and opacity are set together")
        return self


class ReviewHead(Contract):
    aweme_id: AwemeId
    generation: NonNegativeInt
    revision_id: RevisionId
    manifest_key: StorageKey
    manifest_sha256: Sha256
    source: SourceSnapshot

    @model_validator(mode="after")
    def _revision_path(self) -> "ReviewHead":
        expected = f"review/{self.aweme_id}/g{self.generation}/r{self.revision_id}/manifest.json"
        if self.manifest_key != expected:
            raise ValueError("manifest key differs from HEAD identity")
        return self


class ProjectRef(Contract):
    project_key: StorageKey
