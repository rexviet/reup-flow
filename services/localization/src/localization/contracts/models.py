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


class DubbedScriptRef(LocalizedScriptRef):
    """The script the Dubber's decisions produced. `refused_ids` are the lines whose
    rewrite was thrown away (it carried a contact or a character the caption font
    cannot show) and which kept their earlier text; a run with any of them is not
    published without a person looking."""

    refused_ids: tuple[NonNegativeInt, ...] = ()


class LocalizedLine(Contract):
    """One translated line as the Localizer returns it: the text only. Timing,
    slot and source text come from the transcript, never from the model."""

    id: NonNegativeInt
    text_vi: Text


class LocalizedLines(Contract):
    lines: tuple[LocalizedLine, ...]


class ScriptResult(Contract):
    """What the Localizer stage leaves behind. `needs_human` is raised when a
    translated line carries a contact (URL, domain, phone number) the source line
    did not; the run must then not be published without a person looking."""

    script: LocalizedScriptRef
    needs_human: bool
    flagged_segment_ids: tuple[NonNegativeInt, ...]


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


class DubSegmentsDocument(Contract):
    """Stored at `DubbedAudio.segments_key`: how every segment of the script is
    fitted into its slot. One entry per script segment, in script order."""

    aweme_id: AwemeId
    script_version: NonNegativeInt
    segments: tuple[DubSegment, ...]


class VoiceProfile(Contract):
    """What the run knows about the voice before it speaks: the speech engine the
    provider reports as active. It is part of every TTS cache key, so it is read
    once, recorded in the workflow history, and reused for the whole run."""

    engine: str | None


class SynthesisPlan(Contract):
    """The distinct TTS hashes a script needs, in order of first use."""

    hashes: tuple[Sha256, ...]


class SynthClip(Contract):
    hash: Sha256
    key: Key
    duration_ms: PositiveInt


class FitReport(Contract):
    """Segments whose reading does not fit their slot even at the fastest allowed
    stretch: the ones the Dubber has to shorten."""

    unfit_ids: tuple[NonNegativeInt, ...]


class DubDecision(Contract):
    id: NonNegativeInt
    text_vi: Text
    stretch_ratio: Annotated[float, Field(ge=STRETCH_MIN, le=STRETCH_MAX)]


class DubDecisions(Contract):
    items: tuple[DubDecision, ...]


class DubbedAudio(Contract):
    audio_key: Key
    audio_sha256: Sha256
    script_key: Key
    script_version: NonNegativeInt
    segments_key: Key


class LocalizationResult(Contract):
    """What a run returns. `script` is None for a run that began before the
    Localizer stage existed."""

    prep: PrepResult
    script: ScriptResult | None
    # None for a run that began before the dubbing stage existed.
    dub: DubbedAudio | None = None
    # None for a run that began before the review and publish steps existed.
    publish: "PublishResult | None" = None


# --- Render -----------------------------------------------------------------


HexColor = Annotated[str, Field(pattern=r"^#[0-9a-f]{6}$")]


class Cover(Contract):
    """Region painted over the burned-in source subtitle. The colour and opacity are
    part of the spec (and so of its hash): a render made with another colour is not
    the same render. `opacity_pct` is an integer so the spec holds no float, which
    two languages would write differently."""

    region: BBox
    color: HexColor
    opacity_pct: Annotated[int, Field(ge=0, le=100)]


class Caption(Contract):
    segment_id: NonNegativeInt
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    # Lines are separated by "\n": the layout decides where a line ends, the renderer
    # draws exactly those lines at exactly this size.
    text: Text
    font_px: PositiveInt

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "Caption":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not be before start_ms")
        return self


# What the renderer will agree to draw. The numbers come from ffprobe on a file somebody
# else made, so each has a ceiling: without one a crafted file holds the single render
# slot for hours.
MAX_FRAME_PX = 4096
# The render's memory and scratch budget was measured on a 1080x1920 clip and sized for
# frames up to this many pixels: a long side alone does not bound it.
MAX_FRAME_AREA = 1920 * 1088
MAX_FPS = 120
MAX_DURATION_MS = 15 * 60 * 1000
MAX_CAPTIONS = 2000


class EditSpec(Contract):
    source_key: Key
    audio_key: Key
    audio_sha256: Sha256
    script_version: NonNegativeInt
    width: Annotated[int, Field(gt=0, le=MAX_FRAME_PX)]
    height: Annotated[int, Field(gt=0, le=MAX_FRAME_PX)]
    fps: Annotated[int, Field(gt=0, le=MAX_FPS)]
    duration_ms: Annotated[int, Field(gt=0, le=MAX_DURATION_MS)]
    cover: Cover | None
    # Where captions are centred: the cover when there is one, else a safe zone.
    caption_box: BBox
    captions: Annotated[tuple[Caption, ...], Field(max_length=MAX_CAPTIONS)]
    font: Annotated[str, Field(min_length=1, max_length=256)]
    # The font's identity is its bytes, not its name: layout measured with one file, and
    # the renderer refuses to draw with another.
    font_sha256: Sha256

    @model_validator(mode="after")
    def _frame_fits_the_render_budget(self) -> "EditSpec":
        if self.width * self.height > MAX_FRAME_AREA:
            raise ValueError(f"{self.width}x{self.height} is over {MAX_FRAME_AREA} pixels")
        return self


class EditSpecRef(Contract):
    """Where a laid-out `EditSpec` was stored, and what the renderer will key its
    output by. `spec_sha256` is the hash of the canonical spec."""

    spec_key: Key
    spec_sha256: Sha256
    script_version: NonNegativeInt
    audio_sha256: Sha256


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


class SegmentRoom(Contract):
    """The time one line really has: until the end of the video, and its slot (which
    is never below the slot floor, so near the end of the video it can promise more
    than the video has)."""

    id: NonNegativeInt
    to_end_ms: NonNegativeInt
    slot_ms: NonNegativeInt


class SegmentRooms(Contract):
    items: tuple[SegmentRoom, ...]


class ReviewVerdict(Contract):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    passed: Annotated[bool, Field(alias="pass")]
    scores: ReviewScores
    issues: tuple[ReviewIssue, ...]


class RenderInput(Contract):
    """What the render worker (queue `render`, TypeScript) is asked."""

    aweme_id: AwemeId
    spec_key: Key


class RenderResult(Contract):
    """What it answers: the mp4 for this spec, named by the spec's hash."""

    render_key: Key
    spec_sha256: Sha256


PublishOutcome = Literal["published", "needs_human", "failed"]


class PublishRequest(Contract):
    """How a run ended. `needs_human` and `failed` runs are never written to
    `output/`: the first keeps its render under `review/`, the second has none.
    `reasons` are for the person who reads the run (what was flagged, what failed)."""

    outcome: PublishOutcome
    render_key: Key | None = None
    reasons: Annotated[tuple[Annotated[str, Field(max_length=500)], ...], Field(max_length=20)] = ()
    review_rounds: NonNegativeInt = 0
    verdict: ReviewVerdict | None = None


class PublishResult(Contract):
    outcome: PublishOutcome
    output_key: Key | None = None
    review_key: Key | None = None


# `LocalizationResult` names `PublishResult`, which is defined after it.
LocalizationResult.model_rebuild()
