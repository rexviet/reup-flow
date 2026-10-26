"""Step 2a: speech to text on the isolated vocals, regrouped by silence."""

import asyncio

from temporalio import activity

from localization.activities.errors import provider_errors
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
from localization.storage.object_store import ObjectStore, work_key

# A pause longer than this ends a segment.
SILENCE_GAP_MS = 400
# Stays under the contract's 4000 character text limit.
MAX_SEGMENT_CHARS = 3500


def _is_cjk(ch: str) -> bool:
    return "　" <= ch <= "鿿" or "＀" <= ch <= "￯"


def _join(tokens: list[str]) -> str:
    """CJK words run together; other words are separated by a space."""
    text = ""
    for token in tokens:
        if text and not (_is_cjk(text[-1]) or _is_cjk(token[0])):
            text += " "
        text += token
    return text


def regroup(doc: TranscriptDocument) -> TranscriptDocument:
    """Rebuilds the segments from the words: a gap over `SILENCE_GAP_MS` starts a
    new one. With no word timestamps the provider's own segments are kept."""
    words = [w for w in doc.words if w.text]
    if not words:
        return doc
    groups: list[list[SttWord]] = [[words[0]]]
    for word in words[1:]:
        previous = groups[-1][-1]
        size = len(_join([w.text for w in groups[-1]])) + len(word.text) + 1
        if word.start_ms - previous.end_ms > SILENCE_GAP_MS or size > MAX_SEGMENT_CHARS:
            groups.append([word])
        else:
            groups[-1].append(word)
    segments = tuple(
        SttSegment(
            id=i,
            start_ms=group[0].start_ms,
            end_ms=group[-1].end_ms,
            text=_join([w.text for w in group]),
        )
        for i, group in enumerate(groups)
    )
    return TranscriptDocument(
        language=doc.language,
        duration_ms=doc.duration_ms,
        segments=segments,
        words=tuple(words),
    )


class TranscribeActivity:
    def __init__(self, store: ObjectStore, stt: SttProvider) -> None:
        self._store = store
        self._stt = stt

    @activity.defn(name="transcribe")
    async def transcribe(self, spec: LocalizationInput, vocals_key: str) -> Transcript:
        tag_session(spec.aweme_id)
        # `ingest` produced the vocals; if they are gone the workflow re-runs it.
        audio = await asyncio.to_thread(self._store.get_bytes, vocals_key, produced_by="ingest")
        with provider_errors():
            doc = regroup(await self._stt.transcribe(audio, filename="vocals.wav"))
        key = work_key(spec.aweme_id, 0, "transcript.json")
        await asyncio.to_thread(put_json, self._store, key, doc)
        return Transcript(transcript_key=key, segment_count=len(doc.segments))
