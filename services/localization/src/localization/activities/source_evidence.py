"""Give the translator source pixels as independently read, timestamped text."""

import asyncio
import hashlib
import json
import math
import re
import tempfile
from functools import partial
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import run_blocking, run_ffmpeg
from localization.activities.localizer_ports import read_subtitle_document, read_transcript_document
from localization.activities.synthesize import background_heartbeat
from localization.agents.localizer import LocalizerDeps
from localization.contracts.limits import ensure_payload_fits
from localization.contracts.source_evidence import SourceEvidence, SourceEvidenceDocument
from localization.storage.object_store import ArtifactMissingError

PROMPT_VERSION = "source-subtitles-2"


def sample_times(start_ms: int, end_ms: int) -> tuple[int, ...]:
    """Sample changing subtitles across a speech span, not just one middle frame."""
    duration = max(1, end_ms - start_ms)
    count = min(8, max(1, math.ceil(duration / 700)))
    if count == 1:
        return (start_ms + duration // 2,)
    padding = min(100, duration // 4)
    first, last = start_ms + padding, end_ms - 1 - padding
    return tuple(
        dict.fromkeys(first + round((last - first) * index / (count - 1)) for index in range(count))
    )


def source_frame(video: Path, time_ms: int, output: Path, band=None) -> bytes:
    filters = "scale=768:-2"
    if band is not None:
        top, height = band
        filters = f"crop=iw:{height}:0:{top},scale=1024:-2"
    run_ffmpeg(
        [
            "-ss",
            f"{time_ms / 1000:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-an",
            "-vf",
            filters,
            "-q:v",
            "3",
            str(output),
        ],
        timeout=60,
    )
    try:
        return output.read_bytes()
    finally:
        output.unlink(missing_ok=True)


class SourceEvidenceActivity:
    def __init__(self, store, reader):
        self.store = store
        self.reader = reader

    @activity.defn(name="localizer_source_evidence")
    async def collect(self, deps: LocalizerDeps) -> SourceEvidenceDocument:
        prefix = deps.transcript_key.rsplit("/", 1)[0] + "/"
        if (
            not deps.contextual
            or re.fullmatch(rf"work/{deps.aweme_id}/g[0-9]+/r0/", prefix) is None
            or deps.source_video_key != prefix + "source.mp4"
        ):
            raise ApplicationError(
                "source evidence requires the captured generation source",
                type="InvalidSourceEvidence",
                non_retryable=True,
            )
        with provider_errors(), tempfile.TemporaryDirectory(prefix="source-evidence-") as temp:
            root = Path(temp)
            video = root / "source.mp4"
            await run_blocking(
                partial(self.store.download_file, produced_by="capture_review_source"),
                deps.source_video_key,
                video,
            )
            transcript = await asyncio.to_thread(
                read_transcript_document, self.store, deps.transcript_key
            )
            subtitles = await asyncio.to_thread(
                read_subtitle_document, self.store, deps.subtitle_key
            )
            band = None
            if subtitles.region is not None:
                region = subtitles.region
                padding = max(16, region.h // 2)
                top = max(0, region.y - padding)
                bottom = min(subtitles.height, region.y + region.h + padding)
                band = (top, bottom - top)
            semaphore = asyncio.Semaphore(4)

            async def read(segment):
                times = sample_times(segment.start_ms, segment.end_ms)
                identity = json.dumps(
                    [
                        PROMPT_VERSION,
                        self.reader.model_id,
                        deps.source_video_key,
                        segment.id,
                        times,
                        band,
                    ]
                ).encode()
                key = prefix + "source-evidence/" + hashlib.sha256(identity).hexdigest() + ".json"
                try:
                    cached = await asyncio.to_thread(
                        self.store.get_bytes, key, produced_by="localizer_source_evidence"
                    )
                except ArtifactMissingError:
                    pass
                else:
                    return SourceEvidence.model_validate_json(cached)
                async with semaphore:
                    samples = []
                    for time_ms in times:
                        frame = await run_blocking(
                            source_frame, video, time_ms, root / f"{segment.id}-{time_ms}.jpg", band
                        )
                        samples.append((time_ms, frame))
                    result = await self.reader.read_subtitles(samples)
                    # Repeated frames supply no new words. Preserve the first timestamp
                    # of every distinct reading; all evidence remains source data.
                    seen, readings = set(), []
                    for reading in result.readings:
                        if reading.text.strip() and reading.text not in seen:
                            seen.add(reading.text)
                            readings.append(reading)
                    evidence = SourceEvidence(segment_id=segment.id, readings=tuple(readings))
                    await asyncio.to_thread(
                        self.store.put_immutable_bytes, key, evidence.model_dump_json().encode()
                    )
                    return evidence

            async with background_heartbeat():
                evidence = await asyncio.gather(
                    *(
                        read(segment)
                        for segment in transcript.segments
                        if segment.id in deps.expected_ids()
                    ),
                    return_exceptions=True,
                )
            for result in evidence:
                if isinstance(result, BaseException):
                    raise result
            document = SourceEvidenceDocument(evidence=tuple(evidence))
            ensure_payload_fits(document, "timestamped source evidence")
            return document
