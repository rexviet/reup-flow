"""Data that crosses a Temporal boundary, or is persisted as an artifact.

Payload models carry object keys, never bodies (bytes never ride a Temporal
payload). The `*Document` models describe what is stored under those keys.
"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from localization.contracts.base import Contract
from localization.contracts.geometry import BBox, NonNegativeInt, PositiveInt
from localization.contracts.ids import AwemeId, ChannelId, Sha256, SourceKey, StorageKey
from localization.contracts.voice import VoiceRef

Key = StorageKey
Text = Annotated[str, Field(max_length=4000)]


class LocalizationInput(Contract):
    aweme_id: AwemeId
    channel_id: ChannelId
    generation: NonNegativeInt
    video_key: SourceKey
    target_lang: Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")]
    voice: VoiceRef


# --- Transcript -------------------------------------------------------------


class SttWord(Contract):
    text: Annotated[str, Field(max_length=200)]
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "SttWord":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not be before start_ms")
        return self


class SttSegment(Contract):
    id: NonNegativeInt
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    text: Text

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "SttSegment":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not be before start_ms")
        return self


class TranscriptDocument(Contract):
    """Stored at `Transcript.transcript_key`."""

    language: str | None
    duration_ms: NonNegativeInt | None
    segments: tuple[SttSegment, ...]
    words: tuple[SttWord, ...]


class Transcript(Contract):
    transcript_key: Key
    segment_count: NonNegativeInt


class SubtitleTrack(Contract):
    subtitle_key: Key
    region: BBox | None


class SubtitleCue(Contract):
    """A span of the source video during which a burned-in subtitle is on screen."""

    start_ms: NonNegativeInt
    end_ms: NonNegativeInt

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "SubtitleCue":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not be before start_ms")
        return self


class SubtitleDocument(Contract):
    """Stored at `SubtitleTrack.subtitle_key`. `region` is None when the video has
    no burned-in subtitles, and then `cues` is empty."""

    width: PositiveInt
    height: PositiveInt
    region: BBox | None
    cues: tuple[SubtitleCue, ...]


# --- Preparation ------------------------------------------------------------


class IngestResult(Contract):
    vocals_key: Key
    bgm_key: Key
    duration_ms: NonNegativeInt
    width: PositiveInt
    height: PositiveInt
    fps: PositiveInt


class PrepResult(Contract):
    """What the preparation stages leave for the stages after them."""

    ingest: IngestResult
    transcript: Transcript
    subtitles: SubtitleTrack


# --- Localization -----------------------------------------------------------


class LocalizedSegment(Contract):
    id: NonNegativeInt
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    slot_ms: NonNegativeInt
    src_zh: Text
    text_vi: Text
    on_screen: bool

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "LocalizedSegment":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not be before start_ms")
        return self


class LocalizedScript(Contract):
    """Stored at `LocalizedScriptRef.script_key`."""

    aweme_id: AwemeId
    version: NonNegativeInt
    prompt_version: Annotated[str, Field(min_length=1, max_length=64)]
    segments: tuple[LocalizedSegment, ...]


class LocalizedScriptRef(Contract):
    script_key: Key
    version: NonNegativeInt


# --- Dubbing ----------------------------------------------------------------

STRETCH_MIN = 0.85
STRETCH_MAX = 1.15


class DubSegment(Contract):
    id: NonNegativeInt
    text_vi: Text
    tts_key: Key
    duration_ms: NonNegativeInt
    stretch_ratio: Annotated[float, Field(ge=STRETCH_MIN, le=STRETCH_MAX)]
    overflow_unresolved: bool


class DubbedAudio(Contract):
    audio_key: Key
    audio_sha256: Sha256
    script_key: Key
    script_version: NonNegativeInt
    segments_key: Key


# --- Render -----------------------------------------------------------------


class Cover(Contract):
    """Region painted over the burned-in source subtitle."""

    region: BBox


class Caption(Contract):
    segment_id: NonNegativeInt
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    text: Text

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "Caption":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not be before start_ms")
        return self


class EditSpec(Contract):
    source_key: Key
    audio_key: Key
    audio_sha256: Sha256
    script_version: NonNegativeInt
    width: PositiveInt
    height: PositiveInt
    fps: PositiveInt
    duration_ms: PositiveInt
    cover: Cover | None
    captions: tuple[Caption, ...]
    font: Annotated[str, Field(min_length=1, max_length=256)]


# --- Review -----------------------------------------------------------------

Score = Annotated[float, Field(ge=0.0, le=1.0)]
Stage = Literal["transcribe", "detect_subtitles", "localize", "dub", "captions", "render"]


class ReviewScores(Contract):
    fidelity: Score
    timing: Score
    visual: Score


class ReviewIssue(Contract):
    segment_id: NonNegativeInt | None = None
    stage: Stage
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ReviewVerdict(Contract):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    passed: Annotated[bool, Field(alias="pass")]
    scores: ReviewScores
    issues: tuple[ReviewIssue, ...]
