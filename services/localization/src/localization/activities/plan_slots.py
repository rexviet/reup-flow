"""Where each line of the Vbee dub is spoken.

Vbee's speech-to-text returns a few long utterances with only their start and end, so
where each sentence is said comes from the separated vocals: ffmpeg finds the pauses,
the speech between them is merged into slots of about 3 to 10 s cut only at pauses, and
each utterance's text is shared over the slots inside its span by code, in proportion
to speech time. Every cut then moves to the nearest phrase end, so no line stops on a
dangling word. A model was tried for this split and drifted, dropping the last 20 s.
"""

import asyncio
import math
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import probe_duration_ms, run_blocking, run_ffmpeg
from localization.contracts import LocalizationInput
from localization.contracts.vbee_flow import Slot, SlotPlan, SpeechTranscript, Utterance
from localization.observability.tracing import tag_session
from localization.storage.object_store import ObjectStore
from localization.text.vi_syllables import count_syllables

# -30 dB with 0.15 s finds the breath between phrases even in fast continuous speech
# (one every 1.5 to 3 s); a stricter -35 dB / 0.25 s found no pause in 27 s of real
# speech and would force a slot past 10 s. Extra candidate cuts are harmless: the slot
# programme picks among them and prefers the long ones.
SILENCE_DB = -30
SILENCE_MIN_S = 0.15
# Less speech than this in the whole vocals track is nothing to dub. Tracks with no
# voice measure 0 s here; a music track where two sung words cross -30 dB measured
# 1.35 s; the shortest speaking video, 10.2 s.
MIN_SPEECH_MS = 2000
SLOT_MIN_MS = 3000
SLOT_MAX_MS = 10_000
SLOT_TARGET_MS = 6000
# A line may run this far into the pause after its speech, never further: past it
# nobody is speaking in the picture.
PAUSE_SLACK_MS = 1000
# A pause this long is as good a cut as any longer one: a sentence end, not a breath.
LONG_PAUSE_MS = 600
# The slot programme does not consider slots longer than this, unless nothing shorter
# exists; it keeps the search linear on a long video.
SLOT_SEARCH_MS = 60_000
# How far a cut may move, in words, to reach a phrase end. About a second of speech:
# further and the line is no longer said over its own picture.
MAX_SHIFT_WORDS = 8

PHRASE_END = ".,!?;:…"
_CLOSERS = "\"'”’)]»"
_NUMBER = re.compile(r"\d(?:[\d.,]*\d)?")
_SILENCE_START = re.compile(r"silence_start: (-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end: (-?[\d.]+)")


class Span(NamedTuple):
    start_ms: int
    end_ms: int


# --- speech in the vocals --------------------------------------------------------------


def parse_silences(log: str, audio_ms: int) -> list[Span]:
    """The silences in an ffmpeg `silencedetect` log. A silence still open at the end
    of the file runs to its end."""
    starts = [float(m) for m in _SILENCE_START.findall(log)]
    ends = [float(m) for m in _SILENCE_END.findall(log)]
    if len(ends) == len(starts) - 1:
        ends.append(audio_ms / 1000)
    if len(ends) != len(starts):
        raise ValueError("silencedetect: unmatched silence start and end")
    spans = []
    for a, b in zip(starts, ends, strict=True):
        start = min(audio_ms, max(0, round(a * 1000)))
        end = min(audio_ms, max(start, round(b * 1000)))
        if end > start:
            spans.append(Span(start, end))
    return spans


def speech_chunks(silences: Sequence[Span], audio_ms: int) -> list[Span]:
    """What the silences leave of [0, audio_ms]: the stretches of speech, in order."""
    chunks, t = [], 0
    for silence in sorted(silences):
        if silence.start_ms > t:
            chunks.append(Span(t, silence.start_ms))
        t = max(t, silence.end_ms)
    if audio_ms > t:
        chunks.append(Span(t, audio_ms))
    return chunks


def detect_speech(vocals: Path) -> tuple[list[Span], int]:
    """The speech chunks of a vocals file and its length in ms."""
    audio_ms = probe_duration_ms(vocals)
    result = run_ffmpeg(
        ["-i", str(vocals), "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_S}"]
        + ["-f", "null", "-"],
        loglevel="info",
    )
    log = result.stderr.decode(errors="replace")
    return speech_chunks(parse_silences(log, audio_ms), audio_ms), audio_ms


# --- slots -----------------------------------------------------------------------------


def _slot_ranges(chunks: Sequence[Span], audio_ms: int) -> list[tuple[int, int, Span]]:
    """(first chunk, last chunk, span) of each slot. A dynamic programme picks the
    pauses to cut at: a slot's cost grows with its distance from 6 s, steeply outside
    3 to 10 s, and falls when it ends at a long pause."""
    n = len(chunks)

    def end_of(last: int) -> int:
        stop = chunks[last + 1].start_ms if last + 1 < n else audio_ms
        return min(stop, chunks[last].end_ms + PAUSE_SLACK_MS)

    def cost(first: int, last: int) -> float:
        length = end_of(last) - chunks[first].start_ms
        value = ((length - SLOT_TARGET_MS) / 3000) ** 2
        if length < SLOT_MIN_MS:
            value += 10 + ((SLOT_MIN_MS - length) / 500) ** 2
        if length > SLOT_MAX_MS:
            value += 10 + ((length - SLOT_MAX_MS) / 500) ** 2
        if last + 1 < n:
            pause = chunks[last + 1].start_ms - chunks[last].end_ms
            value -= 1.5 * min(pause, LONG_PAUSE_MS) / LONG_PAUSE_MS
        return value

    # best[j]: the cheapest way to cut chunks[:j] into slots; back[j]: its last slot's
    # first chunk.
    best = [0.0] + [math.inf] * n
    back = [0] * (n + 1)
    for j in range(1, n + 1):
        for first in range(j - 1, -1, -1):
            too_long = chunks[j - 1].end_ms - chunks[first].start_ms > SLOT_SEARCH_MS
            if too_long and first < j - 1:
                break
            value = best[first] + cost(first, j - 1)
            if value < best[j]:
                best[j], back[j] = value, first
    ranges, j = [], n
    while j > 0:
        first = back[j]
        ranges.append((first, j - 1, Span(chunks[first].start_ms, end_of(j - 1))))
        j = first
    return ranges[::-1]


def group_slots(chunks: Sequence[Span], audio_ms: int) -> list[Span]:
    """The speech chunks merged into slots of about 3 to 10 s, cut only at pauses. A
    slot runs from its first word to the next slot's start, or at most `PAUSE_SLACK_MS`
    past its last word."""
    return [span for _, _, span in _slot_ranges(chunks, audio_ms)]


# --- text over the slots ---------------------------------------------------------------


def _overlap(a: Span, b: Span) -> int:
    return max(0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))


def _handover(before: Span, after: Span, chunks: Sequence[Span]) -> int:
    """Where one utterance hands over to the next when Vbee's spans overlap: in the
    longest pause inside the overlap, else at its middle."""
    lo, hi = after.start_ms, before.end_ms
    pauses = [Span(a.end_ms, b.start_ms) for a, b in zip(chunks, chunks[1:], strict=False)]
    inside = [
        Span(max(lo, p.start_ms), min(hi, p.end_ms)) for p in pauses if _overlap(p, Span(lo, hi))
    ]
    if inside:
        pause = max(inside, key=lambda p: p.end_ms - p.start_ms)
        return pause.end_ms
    return (lo + hi) // 2


def _spans(utterances: Sequence[Utterance], chunks: Sequence[Span]) -> list[Span]:
    """Each utterance's span with overlaps with its neighbours resolved, so no stretch
    of speech is counted for two utterances."""
    spans = [Span(u.start_ms, u.end_ms) for u in utterances]
    for i in range(1, len(spans)):
        before, after = spans[i - 1], spans[i]
        if after.start_ms < before.end_ms:
            cut = max(before.start_ms, _handover(before, after, chunks))
            spans[i - 1] = Span(before.start_ms, cut)
            spans[i] = Span(cut, max(cut, after.end_ms))
    return spans


def _stripped(word: str) -> str:
    return word.rstrip(_CLOSERS)


def _is_number(word: str) -> bool:
    return _NUMBER.fullmatch(_stripped(word).rstrip(PHRASE_END)) is not None


def _cuttable(before: str, after: str, punctuated: bool) -> bool:
    """Whether a line may end after `before` when `after` follows it. In a punctuated
    utterance only at a phrase end; in one without punctuation at any word boundary.
    Never inside a number ("4, 6") nor between a number and the word it counts."""
    end = _stripped(before)
    if _is_number(before) and (after[:1].isdigit() or not end.endswith(tuple(PHRASE_END))):
        return False
    return end.endswith(tuple(PHRASE_END)) if punctuated else True


@dataclass(frozen=True)
class _Word:
    text: str
    utterance: int
    slot: int


def _place_words(
    utterances: Sequence[Utterance], chunks: Sequence[Span], ranges: Sequence[tuple[int, int, Span]]
) -> list[_Word]:
    """Each word with the slot it is said in, assuming even speech within an utterance:
    a word sits at its share of the utterance's syllables along its voiced time. An
    utterance over silent vocals goes to the slot nearest in time."""
    spans = _spans(utterances, chunks)
    placed: list[_Word] = []
    for index, (utterance, span) in enumerate(zip(utterances, spans, strict=True)):
        words = utterance.text.split()
        weights = [max(1, count_syllables(w)) for w in words]
        voiced = [
            sum(_overlap(chunks[c], span) for c in range(first, last + 1))
            for first, last, _ in ranges
        ]
        total = sum(voiced)
        if total == 0:
            middle = (span.start_ms + span.end_ms) / 2
            nearest = min(
                range(len(ranges)),
                key=lambda k: abs((ranges[k][2].start_ms + ranges[k][2].end_ms) / 2 - middle),
            )
            placed += [_Word(w, index, nearest) for w in words]
            continue
        bounds, done = [], 0
        for v in voiced:
            done += v
            bounds.append(done / total)
        slot, said, weight_sum = 0, 0, sum(weights)
        for word, weight in zip(words, weights, strict=True):
            at = (said + weight / 2) / weight_sum
            said += weight
            while slot < len(bounds) - 1 and at > bounds[slot]:
                slot += 1
            placed.append(_Word(word, index, slot))
    return placed


def _allowed_cuts(words: Sequence[_Word], punctuated: Sequence[bool]) -> list[int]:
    """Word indices a line may start at: an utterance's first word, or a word after a
    cuttable boundary inside an utterance."""
    allowed = []
    for x in range(1, len(words)):
        before, after = words[x - 1], words[x]
        if before.utterance != after.utterance or _cuttable(
            before.text, after.text, punctuated[before.utterance]
        ):
            allowed.append(x)
    return allowed


@dataclass(frozen=True)
class _Cut:
    """A cut between two slots: the word index time put it at, and where it went
    (None when no phrase end was near enough and the two slots became one)."""

    wanted: int
    at: int | None


def _cut_at_phrases(words: Sequence[_Word], slot_count: int, allowed: Sequence[int]) -> list[_Cut]:
    """Each cut between slots, moved to the nearest allowed word index within
    `MAX_SHIFT_WORDS` (ties go earlier), always after the previous cut and before the
    last word, so every slot keeps at least one word."""
    cuts, prev = [], 0
    for k in range(slot_count - 1):
        wanted = sum(1 for w in words if w.slot <= k)
        options = [x for x in allowed if prev < x and abs(x - wanted) <= MAX_SHIFT_WORDS]
        at = min(options, key=lambda x: (abs(x - wanted), x)) if options else None
        if at is not None:
            prev = at
        cuts.append(_Cut(wanted, at))
    return cuts


def _no_speech(note: str) -> SlotPlan:
    return SlotPlan(slots=(), has_speech=False, notes=(note,))


def plan_slots(chunks: Sequence[Span], audio_ms: int, transcript: SpeechTranscript) -> SlotPlan:
    """The slots of a video and the text spoken in each, from the speech chunks of its
    vocals and Vbee's utterances.

    Speech outside every utterance gets no slot: Vbee heard no words there (a cough, a
    sung note), and a slot there would be either mute or stretched over silence. Where
    no phrase end lies near a cut, the two slots become one, since a line ending on a
    dangling word is worse than a long line, which the next step can shorten."""
    spoken = sum(c.end_ms - c.start_ms for c in chunks)
    if spoken < MIN_SPEECH_MS:
        return _no_speech(f"{spoken} ms of speech in the vocals, under {MIN_SPEECH_MS} ms")
    utterances = [u for u in transcript.utterances if u.text.split()]
    if not utterances:
        return _no_speech("the transcript has no words")
    heard = [Span(u.start_ms, u.end_ms) for u in utterances]
    kept = [c for c in chunks if any(_overlap(c, h) for h in heard)]
    if not kept:
        return _no_speech("no speech in the vocals falls inside the transcript's utterances")

    ranges = _slot_ranges(kept, audio_ms)
    words = _place_words(utterances, kept, ranges)
    punctuated = [
        any(_stripped(w).endswith(tuple(PHRASE_END)) for w in u.text.split()[:-1])
        for u in utterances
    ]
    cuts = _cut_at_phrases(words, len(ranges), _allowed_cuts(words, punctuated))

    slots: list[Slot] = []
    notes: list[str] = []
    start_word, start_ms = 0, ranges[0][2].start_ms
    for k, (_, _, span) in enumerate(ranges):
        cut = cuts[k] if k < len(cuts) else _Cut(len(words), len(words))
        if cut.at is None:
            notes.append(
                f"no phrase end within {MAX_SHIFT_WORDS} words of the cut at "
                f"{span.end_ms / 1000:.2f} s; the slots on either side are one"
            )
            continue
        text = " ".join(w.text for w in words[start_word : cut.at])
        slots.append(Slot(id=len(slots), start_ms=start_ms, end_ms=span.end_ms, text=text))
        if k + 1 < len(ranges):
            start_word, start_ms = cut.at, ranges[k + 1][2].start_ms
    return SlotPlan(slots=tuple(slots), has_speech=True, notes=tuple(notes))


# --- the activity ----------------------------------------------------------------------


def _speech_in(store: ObjectStore, vocals_key: str) -> tuple[list[Span], int]:
    with tempfile.TemporaryDirectory(prefix="plan-slots-") as tmp:
        vocals = Path(tmp) / "vocals.wav"
        store.download_file(vocals_key, vocals, produced_by="ingest")
        return detect_speech(vocals)


class PlanSlotsActivity:
    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    @activity.defn(name="plan_slots")
    async def plan_slots(
        self, spec: LocalizationInput, vocals_key: str, transcript: SpeechTranscript
    ) -> SlotPlan:
        tag_session(spec.aweme_id)
        with provider_errors():
            chunks, audio_ms = await run_blocking(_speech_in, self._store, vocals_key)
        return await asyncio.to_thread(plan_slots, chunks, audio_ms, transcript)
