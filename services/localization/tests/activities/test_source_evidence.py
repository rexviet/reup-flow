from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities import source_evidence as module
from localization.activities.source_evidence import SourceEvidenceActivity, sample_times
from localization.agents.localizer import LocalizerDeps
from localization.contracts.source_evidence import SubtitleReading, SubtitleReadings
from localization.providers.errors import TransientProviderError


@pytest.mark.parametrize("start,end", [(0, 1), (100, 122), (24442, 29642), (0, 60000)])
def test_samples_cover_changing_lines_and_stay_inside_segment(start, end):
    times = sample_times(start, end)
    assert 1 <= len(times) <= 8
    assert list(times) == sorted(set(times))
    assert all(start <= time < end for time in times)
    if end - start > 2000:
        assert times[0] < start + (end - start) * 0.2
        assert times[-1] > start + (end - start) * 0.8


async def test_evidence_is_generation_scoped_cached_and_keeps_multiple_source_lines(
    review_store, monkeypatch
):
    class Reader:
        model_id = "vision-model"
        calls = 0

        async def read_subtitles(self, samples):
            self.calls += 1
            return SubtitleReadings(
                readings=tuple(
                    SubtitleReading(time_ms=t, text="人分三六九等" if i < 3 else "肉分五花三层")
                    for i, (t, _) in enumerate(samples)
                )
            )

    deps = LocalizerDeps(
        aweme_id="7001",
        workflow_id="video-7001-g2",
        channel_id="chan_1",
        transcript_key="work/7001/g2/r0/transcript.json",
        subtitle_key="work/7001/g2/r0/subtitles.json",
        source_video_key="work/7001/g2/r0/source.mp4",
        syllables_per_sec=4,
        segment_count=1,
        contextual=True,
    )
    review_store.put_bytes(deps.source_video_key, b"captured source")
    monkeypatch.setattr(
        module,
        "read_transcript_document",
        lambda *_: SimpleNamespace(segments=[SimpleNamespace(id=0, start_ms=24000, end_ms=30000)]),
    )
    monkeypatch.setattr(module, "source_frame", lambda *_: b"frame pixels")
    monkeypatch.setattr(module, "read_subtitle_document", lambda *_: SimpleNamespace(region=None))
    reader = Reader()
    activity = SourceEvidenceActivity(review_store, reader)
    env = ActivityEnvironment()
    first = await env.run(activity.collect, deps)
    again = await env.run(activity.collect, deps)
    assert first == again and reader.calls == 1
    assert [r.text for r in first.evidence[0].readings] == ["人分三六九等", "肉分五花三层"]
    with pytest.raises(ApplicationError, match="captured generation"):
        await env.run(
            activity.collect,
            deps.model_copy(update={"source_video_key": "work/7001/g1/r0/source.mp4"}),
        )


async def test_retry_repairs_only_failed_reading_and_reuses_completed_segments(
    review_store, monkeypatch
):
    class Reader:
        model_id = "vision-model"
        calls = []
        failed = False

        async def read_subtitles(self, samples):
            first = samples[0][0]
            self.calls.append(first)
            if first < 2000 and not self.failed:
                self.failed = True
                raise TransientProviderError("source OCR: invalid timestamped readings")
            return SubtitleReadings(
                readings=tuple(
                    SubtitleReading(time_ms=time, text="source words") for time, _ in samples
                )
            )

    deps = LocalizerDeps(
        aweme_id="7001",
        workflow_id="source-retry",
        channel_id="chan_1",
        transcript_key="work/7001/g2/r0/transcript.json",
        subtitle_key="work/7001/g2/r0/subtitles.json",
        source_video_key="work/7001/g2/r0/source.mp4",
        syllables_per_sec=4,
        segment_count=2,
        contextual=True,
    )
    review_store.put_bytes(deps.source_video_key, b"source")
    monkeypatch.setattr(
        module,
        "read_transcript_document",
        lambda *_: SimpleNamespace(
            segments=[
                SimpleNamespace(id=0, start_ms=0, end_ms=1000),
                SimpleNamespace(id=1, start_ms=2000, end_ms=3000),
            ]
        ),
    )
    monkeypatch.setattr(module, "read_subtitle_document", lambda *_: SimpleNamespace(region=None))
    monkeypatch.setattr(module, "source_frame", lambda *_: b"frame")
    reader = Reader()
    activity = SourceEvidenceActivity(review_store, reader)
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as error:
        await env.run(activity.collect, deps)
    assert not error.value.non_retryable
    result = await env.run(activity.collect, deps)
    assert len(result.evidence) == 2
    assert len(reader.calls) == 3
