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
# A subtitles-only run hears nothing, so every line comes from the frames, and the cues
# (from a frame every 4 s) cannot say where they are. The whole band is read instead: a
# frame every FULL_STEP_MS, at the middle of its slot, READ_BATCH frames to a request.
FULL_STEP_MS = 500
READ_BATCH = 8
# Two readings are one line misread when at least this alike: one wrong character in a
# line of five. Lines that follow each other share words far more often than that
# (0.6 for two lines of one real dialogue), so the neighbour test's ratio would merge them.
SAME_READING_RATIO = 0.8
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


def _alike(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _plain(a), _plain(b)).ratio()


def _repeats(text: str, neighbours: Sequence[str]) -> bool:
    """Whether a reading is the subtitle of a spoken neighbour still (or already) shown."""
    return any(_alike(text, other) >= SAME_LINE_RATIO for other in neighbours)


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


def full_band_times(duration_ms: int) -> list[int]:
    """The middle of every FULL_STEP_MS slot of the video."""
    return list(range(FULL_STEP_MS // 2, max(1, duration_ms), FULL_STEP_MS))


def lines_from_readings(
    readings: Sequence[SubtitleReading], duration_ms: int
) -> list[tuple[int, int, str]]:
    """(start, end, text) of each line read across the whole video, in order. A line runs
    from half a step before the first frame it is read on to half a step after the last,
    within the video. A blank frame ends it; a reading close enough to it (one misread
    character) continues it."""
    half = FULL_STEP_MS // 2
    lines: list[list] = []
    current: list | None = None
    for reading in sorted(readings, key=lambda r: r.time_ms):
        text = " ".join(reading.text.split())
        if not text:
            current = None
            continue
        if current is not None and _alike(text, current[2]) >= SAME_READING_RATIO:
            current[1] = reading.time_ms
            continue
        current = [reading.time_ms, reading.time_ms, text]
        lines.append(current)
    return [
        (max(0, first - half), min(duration_ms, last + half), text) for first, last, text in lines
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
        if spec.localize_mode == "subtitles_only":
            if band is None:
                return transcript
            return await self._read_whole_band(spec, transcript, doc, subs, band)
        spans = uncovered_spans(subs.cues, doc.segments) if band is not None else []
        if not spans:
            return transcript
        plans = [(span, sample_times(span[0] + EDGE_MS, span[1] - EDGE_MS)) for span in spans]
        results = await self._read(spec, transcript, band, plans)
        added = [
            line
            for (span, _), result in zip(plans, results, strict=True)
            for line in segments_from_readings(
                span, result.readings, _neighbours(span, doc.segments)
            )
        ]
        return await self._commit(spec, transcript, doc, added)

    async def _read_whole_band(self, spec, transcript, doc, subs, band) -> Transcript:
        """A subtitles-only run's transcript: every line read across the whole video."""
        duration = subs.duration_ms or max((c.end_ms for c in subs.cues), default=0)
        times = full_band_times(duration)
        batches = [times[i : i + READ_BATCH] for i in range(0, len(times), READ_BATCH)]
        plans = [((batch[0], batch[-1]), batch) for batch in batches]
        results = await self._read(spec, transcript, band, plans)
        readings = [r for result in results for r in result.readings]
        return await self._commit(spec, transcript, doc, lines_from_readings(readings, duration))

    async def _read(self, spec, transcript, band, plans) -> list[SubtitleReadings]:
        """The readings of each (span, times) plan, in order: each read once, from the
        frames' subtitle band, and kept beside the transcript so a retry reads nothing
        again."""
        video_key = source_video_key(spec)
        prefix = transcript.transcript_key.rsplit("/", 1)[0] + "/"
        keyed = []
        for span, times in plans:
            identity = json.dumps(
                [PROMPT_VERSION, self._reader.model_id, video_key, span, times, band]
            ).encode()
            key = prefix + "subtitle-speech/" + hashlib.sha256(identity).hexdigest() + ".json"
            keyed.append((span, times, key))
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
                    *(read(*plan) for plan in keyed), return_exceptions=True
                )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        return results

    async def _commit(self, spec, transcript, doc, added) -> Transcript:
        """`transcript` with the `added` subtitle lines, as a new document; `transcript`
        itself when there is none."""
        if not added:
            return transcript
        activity.logger.info(
            "%d subtitle line(s) shown with no speech added to the transcript", len(added)
        )
        merged = merge_segments(doc, added)
        key = work_key_for(spec, 0, DOCUMENT_NAME)
        await asyncio.to_thread(put_json, self._store, key, merged)
        return Transcript(transcript_key=key, segment_count=len(merged.segments))
