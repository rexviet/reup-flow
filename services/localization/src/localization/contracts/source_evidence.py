"""Timestamped source text read from video pixels, never translation instructions."""

from typing import Annotated

from pydantic import Field

from localization.contracts.base import Contract


class SubtitleReading(Contract):
    time_ms: Annotated[int, Field(ge=0)]
    text: Annotated[str, Field(max_length=500)]


class SubtitleReadings(Contract):
    readings: Annotated[tuple[SubtitleReading, ...], Field(max_length=8)]


class SourceEvidence(Contract):
    segment_id: Annotated[int, Field(ge=0)]
    readings: Annotated[tuple[SubtitleReading, ...], Field(max_length=8)]


class SourceEvidenceDocument(Contract):
    evidence: Annotated[tuple[SourceEvidence, ...], Field(max_length=150)]
