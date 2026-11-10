"""Step 2a: speech to text on the isolated vocals, regrouped by silence."""

import asyncio
import io
import re
import tempfile
import wave
from pathlib import Path

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import run_blocking, to_stt_audio
from localization.activities.persist import put_json
from localization.contracts import (
    LocalizationInput,
    SttSegment,
    SttWord,
    Transcript,
    TranscriptDocument,
)
from localization.observability.tracing import tag_session
from localization.providers.base import SttProvider
from localization.storage.object_store import ObjectStore, require_work_key, work_key_for

# A pause longer than this ends a segment.
SILENCE_GAP_MS = 400
# One segment becomes one dubbed line, and the TTS cache keeps that line as base64
# in 1,400 bytes of object metadata. Chinese came back as at most 7.13 bytes of it
# per character, so 60 characters (about 11 s of speech) need about 430: a third.
# The provider's word timestamps often run on without a gap, so speech with no
# pause over `SILENCE_GAP_MS` is cut by length instead.
MAX_SEGMENT_CHARS = 60
# A cut leaves no piece shorter than this when it can: a line still worth dubbing.
MIN_SEGMENT_CHARS = 20
SENTENCE_ENDS = "。！？.!?…"
CLAUSE_ENDS = "，、；：,;:"
# A provider can return one number as several words, each ending exactly where the
# next begins: whisperx a word per digit and mark ("3", "0", "岁"; "4", ".", "6"),
# faster-whisper the mark on the digits before it ("4.", "6", 80 ms apart). Words
# with a gap longer than this between them are two numbers, not one.
NUMBER_GAP_MS = 100

_CJK = "　-鿿＀-￯"
# A provider segment without word timestamps is cut into these: one CJK character,
# or a run of anything else up to a space.
_TOKEN = re.compile(rf"[{_CJK}]|[^\s{_CJK}]{{1,200}}")
_NUMBER = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
# A word that can be part of a number, and what the parts must join into: digits, a
# comma before each group of exactly three, and at most one decimal point, last.
_NUMBER_PART = re.compile(r"[0-9.,]+")
_WHOLE_NUMBER = re.compile(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?")
_ASCII_DIGITS = "0123456789"


def _is_cjk(ch: str) -> bool:
    return "　" <= ch <= "鿿" or "＀" <= ch <= "￯"


def _space(before: str, token: str) -> str:
    return "" if not before or _is_cjk(before[-1]) or _is_cjk(token[0]) else " "


def _join(tokens: list[str]) -> str:
    """CJK words run together; other words are separated by a space."""
    text = ""
    for token in tokens:
        text += _space(text, token) + token
    return text


def _close(before: SttWord, after: SttWord) -> bool:
    return after.start_ms - before.end_ms <= NUMBER_GAP_MS


def _starts_with_digit(text: str) -> bool:
    return text != "" and text[0] in _ASCII_DIGITS


def _number_end(words: list[SttWord], i: int) -> int:
    """The index after the longest run of words from `words[i]`, said without a
    pause, that joins into one whole number ("4" "." "6", "4." "6", "1," "000"), and
    does not stop between two digits said on; `i + 1` when there is none."""
    if not (_starts_with_digit(words[i].text) and _NUMBER_PART.fullmatch(words[i].text)):
        return i + 1
    j = i + 1
    while (
        j < len(words) and _NUMBER_PART.fullmatch(words[j].text) and _close(words[j - 1], words[j])
    ):
        j += 1
    for end in range(j, i + 1, -1):
        text = "".join(w.text for w in words[i:end])
        splits_digits = (
            end < j and text[-1] in _ASCII_DIGITS and _starts_with_digit(words[end].text)
        )
        if _WHOLE_NUMBER.fullmatch(text) and not splits_digits:
            return end
    return i + 1


def _numbers(words: list[SttWord]) -> list[SttWord]:
    """The words with each number the provider split into several made one word
    again: digits with no pause between them, a decimal point between digits, and a
    comma before exactly three digits ("1 , 0 0 0"), whether the mark is a word of
    its own or rides on the digits before it ("4." "6"). Every other word is kept."""
    merged: list[SttWord] = []
    i = 0
    while i < len(words):
        j = _number_end(words, i)
        if j == i + 1:
            merged.append(words[i])
        else:
            text = "".join(w.text for w in words[i:j])
            merged.append(
                SttWord(text=text, start_ms=words[i].start_ms, end_ms=words[j - 1].end_ms)
            )
        i = j
    return merged


def _holds(last: SttWord, following: SttWord) -> bool:
    """A number and the word said right after it ("30" "岁", "100" "个") stay in one
    line: split across two, the TTS drops the number."""
    return (
        _NUMBER.fullmatch(last.text) is not None
        and following.text.isalpha()
        and _close(last, following)
    )


def _best_cut(group: list[SttWord]) -> int:
    """How many words the first piece of an over-long run takes. The piece has
    `MIN_SEGMENT_CHARS` to `MAX_SEGMENT_CHARS` characters and leaves at least the
    minimum behind when it can; it ends after a sentence, else after a clause, else
    after the longest pause (the gap, then how long the word is held, which is where
    gapless timestamps put the silence), and otherwise as late as it can."""
    total = len(_join([w.text for w in group]))
    allowed: list[int] = []
    fits: list[int] = []
    leaves_enough: list[int] = []
    length = 0
    for k in range(1, len(group)):
        before = group[k - 2].text if k > 1 else ""
        length += len(_space(before, group[k - 1].text)) + len(group[k - 1].text)
        if length > MAX_SEGMENT_CHARS:
            break
        if _holds(group[k - 1], group[k]):
            continue
        allowed.append(k)
        if length >= MIN_SEGMENT_CHARS:
            fits.append(k)
            rest = total - length - len(_space(group[k - 1].text, group[k].text))
            if rest >= MIN_SEGMENT_CHARS:
                leaves_enough.append(k)

    def score(k: int) -> tuple[int, int, int, int]:
        last, following = group[k - 1], group[k]
        rank = 2 if last.text[-1] in SENTENCE_ENDS else 1 if last.text[-1] in CLAUSE_ENDS else 0
        gap = following.start_ms - last.end_ms
        return rank, gap, following.start_ms - last.start_ms, k

    return max(leaves_enough or fits or allowed or [1], key=score)


def _split(group: list[SttWord]) -> list[list[SttWord]]:
    pieces: list[list[SttWord]] = []
    while len(group) > 1 and len(_join([w.text for w in group])) > MAX_SEGMENT_CHARS:
        cut = _best_cut(group)
        pieces.append(group[:cut])
        group = group[cut:]
    return [*pieces, group]


def _spread(segment: SttSegment) -> list[SttWord]:
    """The segment's text as words, its time shared out by characters."""
    tokens = _TOKEN.findall(segment.text)
    total = sum(len(t) for t in tokens)
    span = segment.end_ms - segment.start_ms
    words, done = [], 0
    for token in tokens:
        start = segment.start_ms + span * done // total
        done += len(token)
        words.append(
            SttWord(text=token, start_ms=start, end_ms=segment.start_ms + span * done // total)
        )
    return words


def _segments(groups: list[list[SttWord]]) -> tuple[SttSegment, ...]:
    return tuple(
        SttSegment(
            id=i,
            start_ms=group[0].start_ms,
            end_ms=group[-1].end_ms,
            text=_join([w.text for w in group]),
        )
        for i, group in enumerate(groups)
    )


def regroup(doc: TranscriptDocument) -> TranscriptDocument:
    """Rebuilds the segments from the words: a gap over `SILENCE_GAP_MS` starts a
    new one, and a run longer than `MAX_SEGMENT_CHARS` is cut. With no word
    timestamps the provider's own segments are kept, the over-long ones cut. A number
    the provider split into digits is one word again, and no cut falls inside it or
    between it and the word said right after it; the stored words stay the provider's."""
    words = [w for w in doc.words if w.text]
    if not words:
        if all(len(s.text) <= MAX_SEGMENT_CHARS for s in doc.segments):
            return doc
        kept: list[SttSegment] = []
        for s in doc.segments:
            if len(s.text) > MAX_SEGMENT_CHARS and _TOKEN.search(s.text):
                kept.extend(_segments(_split(_spread(s))))
            else:
                kept.append(s)
        segments = tuple(s.model_copy(update={"id": i}) for i, s in enumerate(kept))
        return TranscriptDocument(
            language=doc.language, duration_ms=doc.duration_ms, segments=segments, words=()
        )
    units = _numbers(words)
    groups: list[list[SttWord]] = [[units[0]]]
    for word in units[1:]:
        if word.start_ms - groups[-1][-1].end_ms > SILENCE_GAP_MS:
            groups.append([word])
        else:
            groups[-1].append(word)
    return TranscriptDocument(
        language=doc.language,
        duration_ms=doc.duration_ms,
        segments=_segments([piece for group in groups for piece in _split(group)]),
        words=tuple(words),
    )


def _for_stt(stem: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="stt-") as tmp:
        source, converted = Path(tmp) / "stem.wav", Path(tmp) / "stt.wav"
        source.write_bytes(stem)
        to_stt_audio(source, converted)
        return converted.read_bytes()


class TranscribeActivity:
    def __init__(
        self, store: ObjectStore, stt: SttProvider, *, chunked: bool = False
    ) -> None:
        self._store = store
        self._stt = stt
        self._chunked = chunked

    @activity.defn(name="transcribe")
    async def transcribe(self, spec: LocalizationInput, vocals_key: str) -> Transcript:
        tag_session(spec.aweme_id)
        require_work_key(spec, vocals_key)
        # `ingest` produced the vocals; if they are gone the workflow re-runs it.
        stem = await asyncio.to_thread(self._store.get_bytes, vocals_key, produced_by="ingest")
        with provider_errors():
            audio = await run_blocking(_for_stt, stem)
            doc = (
                await self._transcribe_chunks(audio) if self._chunked
                else regroup(await self._stt.transcribe(audio, filename="vocals.wav"))
            )
        key = work_key_for(spec, 0, "transcript.json")
        await asyncio.to_thread(put_json, self._store, key, doc)
        return Transcript(transcript_key=key, segment_count=len(doc.segments))

    async def _transcribe_chunks(self, audio: bytes) -> TranscriptDocument:
        from localization.activities.speech_chunks import split_speech

        chunks = await run_blocking(split_speech, audio)
        with wave.open(io.BytesIO(audio)) as wav:
            duration = round(wav.getnframes() / wav.getframerate() * 1000)
        segments, words, languages = [], [], set()
        for index, chunk in enumerate(chunks):
            doc = regroup(await self._stt.transcribe(chunk.audio, filename=f"chunk-{index}.wav"))
            if doc.language:
                languages.add(doc.language)
            for segment in doc.segments:
                start = chunk.start_ms + segment.start_ms
                end = min(chunk.end_ms, chunk.start_ms + segment.end_ms)
                if start < end and segment.text.strip():
                    segments.append(
                        segment.model_copy(
                            update={
                                "id": len(segments),
                                "start_ms": start,
                                "end_ms": end,
                                "language": doc.language,
                            }
                        )
                    )
            for word in doc.words:
                start = chunk.start_ms + word.start_ms
                end = min(chunk.end_ms, chunk.start_ms + word.end_ms)
                if start < end:
                    words.append(word.model_copy(update={"start_ms": start, "end_ms": end}))
        return TranscriptDocument(
            language=next(iter(languages)) if len(languages) == 1 else None,
            duration_ms=duration, segments=tuple(segments), words=tuple(words),
        )
