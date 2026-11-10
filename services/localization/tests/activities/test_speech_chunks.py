import io
import wave
from unittest.mock import AsyncMock

from localization.activities import speech_chunks
from localization.activities.speech_chunks import chunk_bounds, split_speech
from localization.activities.transcribe import TranscribeActivity
from localization.contracts import SttSegment, SttWord, TranscriptDocument


def wav(seconds=30):
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(bytes(seconds * 32000))
    return out.getvalue()


def test_boundaries_merge_short_gaps_split_long_speech_and_never_duplicate_padding():
    spans = [
        {"start": 1600, "end": 16000},
        {"start": 20000, "end": 40000},
        {"start": 200000, "end": 450000},
    ]
    bounds = chunk_bounds(spans, 480000)
    assert bounds[0] == (0, 43200)
    assert len(bounds) == 3
    assert all(0 <= a < b <= 480000 and b - a <= 192000 for a, b in bounds)
    assert all(a[1] <= b[0] for a, b in zip(bounds, bounds[1:], strict=False))


def test_split_retains_pcm_and_silence_does_not_trigger_stt(monkeypatch):
    monkeypatch.setattr(speech_chunks, "speech_spans", lambda pcm: [])
    assert split_speech(wav()) == []
    monkeypatch.setattr(speech_chunks, "speech_spans", lambda pcm: [{"start": 16000, "end": 32000}])
    (chunk,) = split_speech(wav())
    assert (chunk.start_ms, chunk.end_ms) == (800, 2200)
    with wave.open(io.BytesIO(chunk.audio)) as f:
        assert (f.getnframes(), f.getframerate(), f.getnchannels()) == (22400, 16000, 1)


async def test_chunk_languages_offsets_and_full_duration_survive_regroup(monkeypatch):
    monkeypatch.setattr(
        speech_chunks,
        "speech_spans",
        lambda pcm: [{"start": 16000, "end": 32000}, {"start": 160000, "end": 176000}],
    )
    provider = AsyncMock()
    provider.transcribe.side_effect = [
        TranscriptDocument(
            language=lang,
            duration_ms=1400,
            segments=(SttSegment(id=0, start_ms=0, end_ms=1400, text=text),),
            words=(SttWord(text=text, start_ms=100, end_ms=700),),
        )
        for lang, text in [("ko", "안녕"), ("zh", "你好")]
    ]
    result = await TranscribeActivity(object(), provider, chunked=True)._transcribe_chunks(wav())
    assert result.duration_ms == 30000 and result.language is None
    assert [(s.id, s.language, s.start_ms, s.end_ms) for s in result.segments] == [
        (0, "ko", 900, 1500),
        (1, "zh", 9900, 10500),
    ]
    assert [w.start_ms for w in result.words] == [900, 9900]
    assert provider.transcribe.await_count == 2


async def test_no_speech_yields_empty_transcript_without_provider_calls(monkeypatch):
    monkeypatch.setattr(speech_chunks, "speech_spans", lambda pcm: [])
    provider = AsyncMock()
    result = await TranscribeActivity(object(), provider, chunked=True)._transcribe_chunks(wav())
    assert result.segments == () and result.duration_ms == 30000
    provider.transcribe.assert_not_awaited()


async def test_language_metadata_and_legacy_transcripts_reach_localizer_ports():
    import json

    from localization.activities.localizer_ports import StorePorts

    class Store:
        def get_bytes(self, key, *, produced_by):
            return json.dumps({'language': None, 'duration_ms': 4000, 'words': [], 'segments': [
                {'id': 7, 'start_ms': 0, 'end_ms': 1000, 'text': '안녕', 'language': 'ko'},
                {'id': 8, 'start_ms': 2000, 'end_ms': 3000, 'text': '你好'},
            ]}).encode()

    segments = await StorePorts(Store(), None).segments('work/7001/r0/transcript.json')
    assert [(s.id, s.language) for s in segments] == [(0, 'ko'), (1, None)]
