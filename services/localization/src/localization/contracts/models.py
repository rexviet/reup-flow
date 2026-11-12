"""Data that crosses a Temporal boundary, or is persisted as an artifact.

Payload models carry object keys, never bodies (bytes never ride a Temporal
payload). The `*Document` models describe what is stored under those keys.
"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from localization.contracts.base import Contract
from localization.contracts.geometry import BBox, NonNegativeInt, PositiveInt
from localization.contracts.ids import AwemeId, ChannelId, Sha256, SourceKey, StorageKey
from localization.contracts.review_project import SourceSnapshot
from localization.contracts.voice import REVIEW_PROVIDERS, VoiceRef

Key = StorageKey
Text = Annotated[str, Field(max_length=4000)]
LineStatus = Literal["translated", "inferred", "unresolved"]
LineReason = Annotated[str, Field(min_length=1, max_length=500)]
DeliveryMode = Literal["legacy_render", "review_project"]


class LineMetadata(Contract):
    text_vi: Text
    status: LineStatus = "translated"
    reason: LineReason | None = None

    @model_validator(mode="after")
    def _consistent_line(self) -> "LineMetadata":
        text = self.text_vi.strip()
        if self.status == "translated":
            if self.reason is not None:
                raise ValueError("translated lines must not carry a reason")
            # Old blank JSON still decodes, but remains unusable in the Localizer
            # and script assembler. Only explicit unresolved permits omission.
        elif self.reason is None or not self.reason.strip():
            raise ValueError("inferred/unresolved lines require a reason")
        elif self.status == "unresolved" and self.text_vi != "":
            raise ValueError("unresolved lines must have exactly empty text")
        elif self.status == "inferred" and not text:
            raise ValueError("inferred lines require spoken text")
        return self


class LocalizationInput(Contract):
    delivery_mode: DeliveryMode = "legacy_render"
    source_snapshot: SourceSnapshot | None = None
    aweme_id: AwemeId
    channel_id: ChannelId
    generation: NonNegativeInt
    video_key: SourceKey
    target_lang: Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")]
    voice: VoiceRef

    @model_validator(mode="after")
    def _captured_source_matches_input(self) -> "LocalizationInput":
        snapshot = self.source_snapshot
        if snapshot is not None and (
            self.delivery_mode != "review_project"
            or self.voice.provider not in REVIEW_PROVIDERS
            or snapshot.source_key != self.video_key
            or snapshot.snapshot_key != f"work/{self.aweme_id}/g{self.generation}/r0/source.mp4"
        ):
            raise ValueError("source snapshot differs from workflow input")
        return self


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


SegmentSource = Literal["speech", "subtitle"]
SegmentKind = Literal["speech", "singing"]


class SttSegment(Contract):
    # Detected for the independent speech chunk; not verified language per word.
    language: str | None = None
    id: NonNegativeInt
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    text: Text
    # `subtitle`: no speech was heard here, the text was read from the burned-in subtitle
    # on screen. `singing`: the voice sings, so the dub keeps the original sound there.
    source: SegmentSource = "speech"
    kind: SegmentKind = "speech"

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


class SingingCheck(Contract):
    """What the singing check left: the transcript with each segment's kind, whether a
    model was asked at all (`checked` False: no model is configured, every segment stays
    speech), and the segments found sung."""

    transcript: Transcript
    checked: bool
    singing_ids: tuple[NonNegativeInt, ...] = ()


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
    no burned-in subtitles, and then `cues` is empty.

    `frame_boxes` is what the OCR answered on each sampled frame (in source pixels,
    None for no subtitle or an unusable answer), one every `frame_interval_ms` from the
    start, and `unusable_frames` the indexes of the unusable ones: enough to tell whether
    a wrong region came from the model or from the consensus built on its answers. A
    document written before they existed has neither."""

    width: PositiveInt
    height: PositiveInt
    region: BBox | None
    cues: tuple[SubtitleCue, ...]
    frame_interval_ms: PositiveInt | None = None
    frame_boxes: tuple[BBox | None, ...] = ()
    unusable_frames: tuple[NonNegativeInt, ...] = ()


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


class VoiceCheck(Contract):
    """What the voice check found on the source, before anything else runs. A file without
    a video stream (`has_video` False: sound alone, or a still picture over it) is skipped
    whatever it says, so its audio is not measured and `speech_share` is None. Otherwise
    `speech_share` is the part of the audio a voice-activity detector calls speech (0 for a
    file without audio), and `no_voice` says it is under the worker's threshold. A check that
    is switched off looks at nothing (`speech_share` None, a video stream assumed) and finds
    a voice, so the video is localized."""

    speech_share: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    has_video: bool = True
    no_voice: bool = False


# --- Localization -----------------------------------------------------------


class LocalizedSegment(LineMetadata):
    id: NonNegativeInt
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    slot_ms: NonNegativeInt
    src_zh: Text
    on_screen: bool
    # A sung line: captioned, never spoken; the mix keeps the original vocals there.
    keep_original: bool = False

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


class LocalizedLine(LineMetadata):
    """One line with optional review status/reason. Timing,
    slot and source text come from the transcript, never from the model."""

    id: NonNegativeInt


class LocalizedLines(Contract):
    lines: tuple[LocalizedLine, ...]


class ScriptResult(Contract):
    """What the Localizer stage leaves behind. `needs_human` is raised when a
    translated line carries a contact (URL, domain, phone number) the source line
    did not; the run must then not be published without a person looking."""

    script: LocalizedScriptRef
    needs_human: bool
    flagged_segment_ids: tuple[NonNegativeInt, ...]
    inferred_segment_ids: tuple[NonNegativeInt, ...] = ()
    unresolved_segment_ids: tuple[NonNegativeInt, ...] = ()


# --- Dubbing ----------------------------------------------------------------

STRETCH_MIN = 0.85
STRETCH_MAX = 1.15


class DubSegment(LineMetadata):
    id: NonNegativeInt
    tts_key: Key | None = None
    duration_ms: NonNegativeInt
    stretch_ratio: Annotated[float, Field(ge=STRETCH_MIN, le=STRETCH_MAX)]
    overflow_unresolved: bool
    # The original sound is kept over this segment (it is sung): it has no clip.
    keep_original: bool = False
    # Words of the line the voice left out in every request, so the clip says less than
    # the subtitle: a person has to hear it.
    lost_words: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _omitted_has_no_clip(self) -> "DubSegment":
        if self.status != "unresolved" and not self.keep_original:
            if self.tts_key is None:
                raise ValueError("spoken segments require a TTS clip")
            return self
        if (
            self.tts_key is not None
            or self.duration_ms != 0
            or self.stretch_ratio != 1.0
            or self.overflow_unresolved
        ):
            raise ValueError("unspoken segments must be omitted without a clip or overflow")
        return self


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
    Localizer stage existed, and for a video that was not localized because it is a
    product advertisement (its `publish` says `skipped_ad`). `prep` is None for a video
    without a voice, whose original was published before anything was prepared (its
    `publish` says `published_original`), and for a file without a video stream, skipped
    before anything was prepared (`skipped_no_video`)."""

    prep: PrepResult | None = None
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
    # Old stored specs painted the cover throughout the video. New layouts tie it
    # to caption frames; the explicit flag gives those renders a distinct hash.
    only_with_captions: bool = False


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
    """What the render activity (queue `render`) is asked."""

    aweme_id: AwemeId
    spec_key: Key


class RenderResult(Contract):
    """What it answers: the mp4 for this spec, named by the spec's hash."""

    render_key: Key
    spec_sha256: Sha256


# `skipped_ad`: the video is a product advertisement, worth nothing outside the market it
# was made for, so nothing was localized, rendered or reviewed.
# `published_original`: the video has no voice (music, sounds), so there was nothing to
# translate or dub; the source itself was published, as it is, before any preparation.
# `skipped_no_video`: the file has no video stream (pictures with music laid over them, as an
# image post is), so there is no video to localize or publish; nothing was prepared.
PublishOutcome = Literal[
    "published",
    "needs_human",
    "ready_for_review",
    "failed",
    "skipped_ad",
    "published_original",
    "skipped_no_video",
]


class PublishRequest(Contract):
    """How a run ended. `needs_human`, `failed`, `skipped_ad` and `skipped_no_video` runs
    are never written to `output/`: the first keeps its render under `review/`, the others
    have none. A
    `published_original` run has no render either: what goes to `output/` is the run's
    source video (`LocalizationInput.video_key`), so the request names no key.
    `reasons` are for the person who reads the run (what was flagged, what failed).
    `dub` (the track the render was made from) and `safety_flag` (a line carried a
    contact its source lacked) are what the run's scores are measured from; a request
    recorded before they existed has neither."""

    outcome: PublishOutcome
    project_key: Key | None = None
    render_key: Key | None = None
    reasons: Annotated[tuple[Annotated[str, Field(max_length=500)], ...], Field(max_length=20)] = ()
    review_rounds: NonNegativeInt = 0
    verdict: ReviewVerdict | None = None
    dub: DubbedAudio | None = None
    safety_flag: bool = False


class PublishResult(Contract):
    outcome: PublishOutcome
    project_key: Key | None = None
    output_key: Key | None = None
    review_key: Key | None = None


# `LocalizationResult` names `PublishResult`, which is defined after it.
LocalizationResult.model_rebuild()
