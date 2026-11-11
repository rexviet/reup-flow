"""Burned-in subtitles shown where no speech was heard become transcript segments.

The speech-to-text hears only what the vocals carry; a line the video shows on screen
while nobody is heard saying it (a quiet voice, a line the provider dropped) is lost to
the dub. Each stretch of a subtitle cue that no transcript segment covers is read from
the frames, like the source evidence, and every distinct line read there becomes a
segment with `source = "subtitle"`, translated, captioned and dubbed like the dialogue
around it. The result is a new transcript document beside the first; the first is kept.
"""

import asyncio
import difflib
import hashlib
import json
import re
import tempfile
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import run_blocking
from localization.activities.ingest import download_source
from localization.activities.localizer_ports import read_subtitle_document, read_transcript_document
from localization.activities.persist import put_json
from localization.activities.source_evidence import sample_times, source_frame, subtitle_band
from localization.activities.synthesize import background_heartbeat
from localization.contracts import (
    LocalizationInput,
    SttSegment,
    SubtitleCue,
    SubtitleTrack,
    Transcript,
    TranscriptDocument,
)
from localization.contracts.source_evidence import SubtitleReading, SubtitleReadings
from localization.observability.tracing import tag_session
from localization.storage.object_store import (
    ArtifactMissingError,
    ObjectStore,
    require_work_key,
    source_video_key,
    work_key_for,
)

PROMPT_VERSION = "subtitle-speech-1"
# A stretch shorter than this between two spoken lines is the pause between them, during
# which one of their own subtitles is still (or already) on screen.
MIN_GAP_MS = 1500
# Frames are read this far inside the stretch: a neighbour's subtitle lingers at its edges.
EDGE_MS = 300
# Spans read at once: each is a few ffmpeg seeks and one vision request.
READ_CONCURRENCY = 4
DOCUMENT_NAME = "transcript.subtitles.json"
# A reading this alike to a spoken neighbour's text (ASR and OCR of one line differ by a
# character or two) is that neighbour's subtitle.
SAME_LINE_RATIO = 0.6

_NOT_TEXT = re.compile(r"[\W_]+")


def uncovered_spans(
    cues: Sequence[SubtitleCue], segments: Sequence[SttSegment], min_ms: int = MIN_GAP_MS
) -> list[tuple[int, int]]:
    """The stretches, `min_ms` or longer, during which a subtitle is on screen and no
    segment is spoken; in time order."""
    spoken = sorted((s.start_ms, s.end_ms) for s in segments)
    spans: list[tuple[int, int]] = []
    for cue in sorted(cues, key=lambda c: (c.start_ms, c.end_ms)):
        cursor = cue.start_ms
        for start, end in spoken:
            if end <= cursor or start >= cue.end_ms:
                continue
            if start > cursor:
                spans.append((cursor, start))
            cursor = max(cursor, end)
        if cue.end_ms > cursor:
            spans.append((cursor, cue.end_ms))
    return [(a, b) for a, b in spans if b - a >= min_ms]


def _plain(text: str) -> str:
    return _NOT_TEXT.sub("", unicodedata.normalize("NFKC", text)).lower()


def _repeats(text: str, neighbours: Sequence[str]) -> bool:
    """Whether a reading is the subtitle of a spoken neighbour still (or already) shown."""
    plain = _plain(text)
    return any(
        difflib.SequenceMatcher(None, plain, _plain(other)).ratio() >= SAME_LINE_RATIO
        for other in neighbours
    )


def segments_from_readings(
    span: tuple[int, int], readings: Sequence[SubtitleReading], neighbours: Sequence[str]
) -> list[tuple[int, int, str]]:
    """(start, end, text) of each distinct line read in `span`, in order. A line runs from
    the frame it was first read on (the span's start, for the first) to the next line's
    first frame (the span's end, for the last). Empty readings and the neighbours' own
    subtitles are not lines."""
    runs: list[tuple[int, str]] = []
    for reading in readings:
        text = " ".join(reading.text.split())
        if not text or _repeats(text, neighbours):
            continue
        if runs and runs[-1][1] == text:
            continue
        runs.append((span[0] if not runs else reading.time_ms, text))
    return [
        (start, runs[i + 1][0] if i + 1 < len(runs) else span[1], text)
        for i, (start, text) in enumerate(runs)
    ]


def _neighbours(span: tuple[int, int], segments: Sequence[SttSegment]) -> list[str]:
    before = [s for s in segments if s.end_ms <= span[0]]
    after = [s for s in segments if s.start_ms >= span[1]]
    found = []
    if before:
        found.append(max(before, key=lambda s: s.end_ms).text)
    if after:
        found.append(min(after, key=lambda s: s.start_ms).text)
    return found


def merge_segments(
    doc: TranscriptDocument, added: Sequence[tuple[int, int, str]]
) -> TranscriptDocument:
    """`doc` with `added` as subtitle segments, all in time order and numbered by position."""
    extra = [
        SttSegment(id=0, start_ms=a, end_ms=b, text=text, language=doc.language, source="subtitle")
        for a, b, text in added
    ]
    ordered = sorted([*doc.segments, *extra], key=lambda s: (s.start_ms, s.end_ms))
    return doc.model_copy(
        update={"segments": tuple(s.model_copy(update={"id": i}) for i, s in enumerate(ordered))}
    )


class CompleteTranscriptActivity:
    def __init__(self, store: ObjectStore, reader) -> None:
        """`reader` reads subtitles from frames (`VisionOcr.read_subtitles`)."""
        self._store = store
        self._reader = reader

    @activity.defn(name="complete_transcript")
    async def complete_transcript(
        self, spec: LocalizationInput, transcript: Transcript, subtitles: SubtitleTrack
    ) -> Transcript:
        """The transcript with a segment for every subtitle line nobody was heard saying;
        `transcript` itself when there is none."""
        tag_session(spec.aweme_id)
        require_work_key(spec, transcript.transcript_key)
        require_work_key(spec, subtitles.subtitle_key)
        doc = await asyncio.to_thread(
            read_transcript_document, self._store, transcript.transcript_key
        )
        subs = await asyncio.to_thread(read_subtitle_document, self._store, subtitles.subtitle_key)
        band = subtitle_band(subs)
        spans = uncovered_spans(subs.cues, doc.segments) if band is not None else []
        if not spans:
            return transcript
        video_key = source_video_key(spec)
        prefix = transcript.transcript_key.rsplit("/", 1)[0] + "/"
        plans = []
        for span in spans:
            times = sample_times(span[0] + EDGE_MS, span[1] - EDGE_MS)
            identity = json.dumps(
                [PROMPT_VERSION, self._reader.model_id, video_key, span, times, band]
            ).encode()
            key = prefix + "subtitle-speech/" + hashlib.sha256(identity).hexdigest() + ".json"
            plans.append((span, times, key))
        with provider_errors(), tempfile.TemporaryDirectory(prefix="subtitle-speech-") as temp:
            root = Path(temp)
            video = root / "source.mp4"
            downloaded = asyncio.Lock()
            semaphore = asyncio.Semaphore(READ_CONCURRENCY)

            async def read(span, times, key) -> SubtitleReadings:
                try:
                    cached = await asyncio.to_thread(
                        self._store.get_bytes, key, produced_by="complete_transcript"
                    )
                except ArtifactMissingError:
                    pass
                else:
                    return SubtitleReadings.model_validate_json(cached)
                async with semaphore:
                    async with downloaded:
                        if not video.exists():
                            await run_blocking(download_source, self._store, video_key, video)
                    samples = [
                        (
                            t,
                            await run_blocking(
                                source_frame, video, t, root / f"{span[0]}-{t}.jpg", band
                            ),
                        )
                        for t in times
                    ]
                    result = await self._reader.read_subtitles(samples)
                    await asyncio.to_thread(
                        self._store.put_immutable_bytes, key, result.model_dump_json().encode()
                    )
                    return result

            async with background_heartbeat():
                results = await asyncio.gather(
                    *(read(*plan) for plan in plans), return_exceptions=True
                )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        added = [
            line
            for (span, _, _), result in zip(plans, results, strict=True)
            for line in segments_from_readings(
                span, result.readings, _neighbours(span, doc.segments)
            )
        ]
        if not added:
            return transcript
        activity.logger.info(
            "%d subtitle line(s) shown with no speech added to the transcript", len(added)
        )
        merged = merge_segments(doc, added)
        key = work_key_for(spec, 0, DOCUMENT_NAME)
        await asyncio.to_thread(put_json, self._store, key, merged)
        return Transcript(transcript_key=key, segment_count=len(merged.segments))
