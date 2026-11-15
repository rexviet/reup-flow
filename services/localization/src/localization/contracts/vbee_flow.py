"""What the Vbee dubbing flow passes between its steps.

Vbee's speech-to-text hears the Chinese and answers in Vietnamese, in a few long
utterances with only their start and end. Where each sentence is said comes from the
pauses in the separated vocals: they cut the speech into slots, and the utterances'
text is shared out over the slots by code. A slot's text is what is spoken in it.
"""

from typing import Annotated

from pydantic import Field, NonNegativeInt, model_validator

from localization.contracts.base import Contract
from localization.contracts.models import (
    DubbedAudio,
    IngestResult,
    PublishResult,
    ScriptResult,
    SubtitleTrack,
    Text,
)

# What a run's `VoiceRef.voice_id` says for the Vbee voice. The id of the cloned voice
# is an account secret, so it never travels in a workflow's history: the activities that
# speak put the configured `VBEE_VOICE_ID` in its place.
VBEE_CONFIGURED_VOICE = "vbee-configured-voice"


class Utterance(Contract):
    """One stretch of speech as Vbee returned it: Vietnamese text and its span."""

    text: Text
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "Utterance":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not be before start_ms")
        return self


class SpeechTranscript(Contract):
    """The whole of what Vbee heard in one vocals track."""

    utterances: tuple[Utterance, ...]
    audio_ms: NonNegativeInt


class Slot(Contract):
    """A span of the video between two pauses, and the line to be spoken in it."""

    id: NonNegativeInt
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    text: Text

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "Slot":
        if self.end_ms <= self.start_ms:
            raise ValueError("a slot must end after it starts")
        return self

    @property
    def slot_ms(self) -> int:
        return self.end_ms - self.start_ms


class SlotPlan(Contract):
    """The slots of one video, in time order. No speech means nothing to dub."""

    slots: tuple[Slot, ...]
    has_speech: bool
    # Ids of slots whose text was shortened to fit, and why a slot still does not fit.
    shortened_ids: tuple[NonNegativeInt, ...] = ()
    unfit_ids: tuple[NonNegativeInt, ...] = ()
    notes: tuple[Annotated[str, Field(max_length=500)], ...] = ()

    @model_validator(mode="after")
    def _in_order_without_overlap(self) -> "SlotPlan":
        ids = [s.id for s in self.slots]
        if ids != list(range(len(ids))):
            raise ValueError("slot ids must be 0..n-1 in order")
        for a, b in zip(self.slots, self.slots[1:], strict=False):
            if b.start_ms < a.end_ms:
                raise ValueError("slots must not overlap")
        return self


class ClipDuration(Contract):
    """How long the clip of one line of a script really is."""

    id: NonNegativeInt
    duration_ms: NonNegativeInt


class ClipDurations(Contract):
    """The clip length of every line of a script, in script order."""

    items: tuple[ClipDuration, ...]


class VbeeDubbingResult(Contract):
    """What a Vbee dubbing run returns: what it was made from and how it ended."""

    ingest: IngestResult
    subtitles: SubtitleTrack
    slots: SlotPlan
    script: ScriptResult
    dub: DubbedAudio
    publish: PublishResult
