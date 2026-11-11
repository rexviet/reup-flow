"""Subtitle lines shown where no speech was heard become transcript segments: the spans a
cue leaves uncovered are read from the frames, once, and merged in time order."""

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities import complete_transcript as module
from localization.activities.complete_transcript import (
    CompleteTranscriptActivity,
    segments_from_readings,
    uncovered_spans,
)
from localization.activities.localizer_ports import read_transcript_document
from localization.contracts import (
    BBox,
    SttSegment,
    SubtitleCue,
    SubtitleDocument,
    SubtitleTrack,
    Transcript,
    TranscriptDocument,
)
from localization.contracts.source_evidence import SubtitleReading, SubtitleReadings
from localization.providers.errors import TransientProviderError
from localization.storage.object_store import work_key
from tests.support.inputs import make_input

SPEC = make_input()
TRANSCRIPT_KEY = work_key(SPEC.aweme_id, 0, "transcript.json")
SUBTITLE_KEY = work_key(SPEC.aweme_id, 0, "subtitles.json")


def seg(i: int, start: int, end: int, text: str = "说话") -> SttSegment:
    return SttSegment(id=i, start_ms=start, end_ms=end, text=text)


def cue(start: int, end: int) -> SubtitleCue:
    return SubtitleCue(start_ms=start, end_ms=end)


# --- the spans ---------------------------------------------------------------------------


def test_the_spans_are_where_a_subtitle_is_shown_and_nobody_speaks() -> None:
    # g1 of 7691886043717039013: one cue over the whole clip, speech missing twice.
    segments = [seg(0, 500, 17_900), seg(1, 21_900, 27_400), seg(2, 31_700, 35_500)]
    assert uncovered_spans([cue(500, 35_500)], segments) == [(17_900, 21_900), (27_400, 31_700)]


def test_a_pause_between_two_spoken_lines_is_not_a_span() -> None:
    segments = [seg(0, 0, 2000), seg(1, 2900, 5000)]
    assert uncovered_spans([cue(0, 5000)], segments) == []


def test_a_span_runs_to_the_edges_of_its_cue_and_never_past_them() -> None:
    segments = [seg(0, 3000, 4000)]
    assert uncovered_spans([cue(0, 6000), cue(9000, 12_000)], segments) == [
        (0, 3000),
        (4000, 6000),
        (9000, 12_000),
    ]


def test_overlapping_segments_are_one_covered_stretch() -> None:
    segments = [seg(0, 1000, 5000), seg(1, 2000, 3000), seg(2, 4500, 9000)]
    assert uncovered_spans([cue(0, 10_000)], segments) == []


def test_no_cue_means_no_span() -> None:
    assert uncovered_spans([], [seg(0, 0, 1000)]) == []


# --- the lines read in a span ------------------------------------------------------------


def reading(time_ms: int, text: str) -> SubtitleReading:
    return SubtitleReading(time_ms=time_ms, text=text)


def test_each_distinct_line_becomes_a_segment_until_the_next_one() -> None:
    readings = [
        reading(18_200, "第一句"),
        reading(19_000, "第一句"),
        reading(20_000, "第二句"),
        reading(21_000, ""),
    ]
    assert segments_from_readings((17_900, 21_900), readings, []) == [
        (17_900, 20_000, "第一句"),
        (20_000, 21_900, "第二句"),
    ]


def test_a_neighbours_own_subtitle_is_not_a_new_line() -> None:
    readings = [
        reading(18_200, "我们走吧！"),
        reading(19_000, "新的一句"),
        reading(21_000, "下一个人说"),
    ]
    lines = segments_from_readings((17_900, 21_900), readings, ["我们走吧", "下一个人说话了"])
    assert lines == [(17_900, 21_900, "新的一句")]


def test_a_span_with_nothing_readable_has_no_line() -> None:
    assert segments_from_readings((0, 3000), [reading(300, ""), reading(2000, " ")], []) == []


# --- the activity ------------------------------------------------------------------------


class Reader:
    model_id = "vision-model"

    def __init__(self, text_at=lambda t: "屏幕上的字", fail_first: bool = False) -> None:
        self.calls: list[list[int]] = []
        self._text_at = text_at
        self._fail = fail_first

    async def read_subtitles(self, samples):
        self.calls.append([t for t, _ in samples])
        if self._fail:
            self._fail = False
            raise TransientProviderError("source OCR: invalid timestamped readings")
        return SubtitleReadings(readings=tuple(reading(t, self._text_at(t)) for t, _ in samples))


REGION = BBox(x=40, y=1500, w=1000, h=100)


def put_inputs(store, segments, cues, region=REGION) -> None:
    doc = TranscriptDocument(language="zh", duration_ms=40_000, segments=segments, words=())
    store.put_bytes(TRANSCRIPT_KEY, doc.model_dump_json().encode())
    subs = SubtitleDocument(width=1080, height=1920, region=region, cues=cues)
    store.put_bytes(SUBTITLE_KEY, subs.model_dump_json().encode())
    store.put_bytes(SPEC.video_key, b"source video")


@pytest.fixture
def frames(monkeypatch):
    taken: list[int] = []

    def frame(video, time_ms, output, band=None):
        assert video.read_bytes() == b"source video"
        assert band is not None
        taken.append(time_ms)
        return b"frame"

    monkeypatch.setattr(module, "source_frame", frame)
    return taken


def run(store, reader):
    activity = CompleteTranscriptActivity(store, reader)
    return ActivityEnvironment().run(
        activity.complete_transcript,
        SPEC,
        Transcript(transcript_key=TRANSCRIPT_KEY, segment_count=3),
        SubtitleTrack(subtitle_key=SUBTITLE_KEY, region=None),
    )


async def test_unspoken_subtitles_are_merged_as_subtitle_segments_in_time_order(
    review_store, frames
) -> None:
    put_inputs(
        review_store,
        (seg(0, 500, 17_900, "一"), seg(1, 21_900, 27_400, "二"), seg(2, 31_700, 35_500, "三")),
        (cue(500, 35_500),),
    )
    reader = Reader(text_at=lambda t: "第一个空白" if t < 25_000 else "第二个空白")
    result = await run(review_store, reader)
    assert result.transcript_key == work_key(SPEC.aweme_id, 0, "transcript.subtitles.json")
    assert result.segment_count == 5
    doc = read_transcript_document(review_store, result.transcript_key)
    assert [(s.start_ms, s.end_ms, s.text, s.source) for s in doc.segments] == [
        (500, 17_900, "一", "speech"),
        (17_900, 21_900, "第一个空白", "subtitle"),
        (21_900, 27_400, "二", "speech"),
        (27_400, 31_700, "第二个空白", "subtitle"),
        (31_700, 35_500, "三", "speech"),
    ]
    assert [s.id for s in doc.segments] == [0, 1, 2, 3, 4]
    # Read inside each span, away from the neighbours' lingering subtitles.
    assert all(18_200 <= t < 21_600 or 27_700 <= t < 31_400 for t in frames)
    # The transcript as heard is kept.
    assert len(read_transcript_document(review_store, TRANSCRIPT_KEY).segments) == 3


async def test_each_span_is_read_once_and_a_failed_read_is_retried_alone(
    review_store, frames
) -> None:
    put_inputs(
        review_store,
        (seg(0, 0, 2000), seg(1, 6000, 8000), seg(2, 12_000, 14_000)),
        (cue(0, 14_000),),
    )
    reader = Reader(fail_first=True)
    with pytest.raises(ApplicationError) as error:
        await run(review_store, reader)
    assert not error.value.non_retryable
    first = await run(review_store, reader)
    again = await run(review_store, reader)
    assert first == again
    assert len(reader.calls) == 3  # one failure, then each of the two spans once


async def test_a_transcript_with_nothing_unspoken_is_returned_as_it_is(
    review_store, frames
) -> None:
    segments = (seg(0, 0, 5000), seg(1, 5000, 9000), seg(2, 9000, 9500))
    put_inputs(review_store, segments, (cue(0, 9000),))
    reader = Reader()
    result = await run(review_store, reader)
    assert result == Transcript(transcript_key=TRANSCRIPT_KEY, segment_count=3)
    assert reader.calls == [] and frames == []


async def test_a_video_without_burned_in_subtitles_is_not_read(review_store, frames) -> None:
    put_inputs(review_store, (seg(0, 0, 1000),), (), region=None)
    reader = Reader()
    result = await run(review_store, reader)
    assert result.transcript_key == TRANSCRIPT_KEY and reader.calls == []


async def test_spans_with_nothing_readable_leave_the_transcript_as_it_is(
    review_store, frames
) -> None:
    put_inputs(review_store, (seg(0, 0, 1000),), (cue(0, 5000),))
    result = await run(review_store, Reader(text_at=lambda t: ""))
    assert result.transcript_key == TRANSCRIPT_KEY


async def test_completing_a_completed_transcript_adds_nothing(review_store, frames) -> None:
    put_inputs(review_store, (seg(0, 0, 1000), seg(1, 5000, 6000)), (cue(0, 6000),))
    first = await run(review_store, Reader())
    activity = CompleteTranscriptActivity(review_store, Reader())
    again = await ActivityEnvironment().run(
        activity.complete_transcript,
        SPEC,
        first,
        SubtitleTrack(subtitle_key=SUBTITLE_KEY, region=None),
    )
    assert again == first
