"""What the Localizer's tools reach on a worker: the object store for the
transcript and subtitle documents, Postgres for the glossary."""

import asyncio
from collections.abc import Sequence

from localization.activities.glossary import GlossaryStore
from localization.agents.localizer import GlossaryScope, SaveResult, SegmentView, TermView
from localization.contracts import SubtitleDocument, TranscriptDocument
from localization.storage.object_store import ObjectStore
from localization.text.slots import slots_ms


def read_transcript_document(store: ObjectStore, key: str) -> TranscriptDocument:
    """The transcript, its segments numbered 0..n-1 in the order they are spoken.

    A transcript rebuilt from word timestamps is numbered that way already; one
    that keeps the provider's own segments carries whatever ids the provider chose
    (it may start at 1, skip or repeat). The Localizer must be able to say which
    ids it needs back, so the ids are the position, here, for every reader."""
    # The transcript was produced by `transcribe`; if it is gone, that is the stage
    # to run again.
    doc = TranscriptDocument.model_validate_json(store.get_bytes(key, produced_by="transcribe"))
    ordered = sorted(doc.segments, key=lambda s: (s.start_ms, s.end_ms))
    segments = tuple(s.model_copy(update={"id": i}) for i, s in enumerate(ordered))
    return doc.model_copy(update={"segments": segments})


def transcript_slots(doc: TranscriptDocument) -> list[int]:
    """The slot of each segment of `doc`, in order: what the model is shown and what
    the script stores."""
    return slots_ms([(s.start_ms, s.end_ms) for s in doc.segments], doc.duration_ms)


def read_subtitle_document(store: ObjectStore, key: str) -> SubtitleDocument:
    return SubtitleDocument.model_validate_json(
        store.get_bytes(key, produced_by="detect_subtitles")
    )


class StorePorts:
    def __init__(self, store: ObjectStore, glossary: GlossaryStore) -> None:
        self._store = store
        self._glossary = glossary

    async def segments(self, transcript_key: str) -> Sequence[SegmentView]:
        doc = await asyncio.to_thread(read_transcript_document, self._store, transcript_key)
        slots = transcript_slots(doc)
        return [
            SegmentView(s.id, s.start_ms, s.end_ms, s.text, slot, s.language)
            for s, slot in zip(doc.segments, slots, strict=True)
        ]

    async def on_screen_spans(self, subtitle_key: str) -> Sequence[tuple[int, int]]:
        doc = await asyncio.to_thread(read_subtitle_document, self._store, subtitle_key)
        return [(c.start_ms, c.end_ms) for c in doc.cues]

    async def lookup_terms(self, scope: GlossaryScope, terms: list[str]) -> Sequence[TermView]:
        rows = await self._glossary.lookup(
            collection_id=scope.collection_id, channel_id=scope.channel_id, terms=terms or None
        )
        return [TermView(r.term_zh, r.term_vi, r.note) for r in rows]

    async def save_term(
        self,
        *,
        scope: GlossaryScope,
        term_zh: str,
        term_vi: str,
        note: str | None,
        aweme_id: str,
        workflow_id: str,
    ) -> SaveResult:
        outcome = await self._glossary.upsert(
            collection_id=scope.collection_id,
            channel_id=scope.channel_id,
            term_zh=term_zh,
            term_vi=term_vi,
            note=note,
            aweme_id=aweme_id,
            workflow_id=workflow_id,
        )
        return SaveResult(outcome.status, outcome.message, outcome.existing)
