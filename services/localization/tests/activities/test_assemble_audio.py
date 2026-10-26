"""The dubbed track: each clip where its segment starts, played at its stretch, the
music ducked under the voice, loudness at the goal, and the length of the video."""

import hashlib
import tempfile
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.assemble_audio import (
    AssembleAudioActivity,
    build_filter_graph,
    parse_loudnorm,
)
from localization.contracts import (
    DubSegment,
    DubSegmentsDocument,
    IngestResult,
    LocalizedScriptRef,
)
from localization.providers.errors import PermanentProviderError
from localization.storage.object_store import ObjectStore, tts_cache_key, work_key
from tests.support.audio import RATE, db, decode, integrated_lufs, level, tone_span_ms
from tests.support.inputs import make_input
from tests.support.tts_fakes import make_wav, put_script, segment

SPEC = make_input()
VIDEO_MS = 8000
MUSIC_HZ = 1000
VOICE_HZ = 300
LOUDNESS = -14.0
PEAK = -1.5


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        yield ObjectStore(client, "reup-media")


def ingest(store: ObjectStore, *, seconds: float = VIDEO_MS / 1000, silent_music=False):
    bgm_key = work_key("7001", 0, "bgm.wav")
    amplitude = 0.0 if silent_music else 0.25
    store.put_bytes(bgm_key, make_wav(seconds, frequency=MUSIC_HZ, amplitude=amplitude))
    return IngestResult(
        vocals_key=work_key("7001", 0, "vocals.wav"),
        bgm_key=bgm_key,
        duration_ms=VIDEO_MS,
        width=1080,
        height=1920,
        fps=30,
    )


def put_clip(store: ObjectStore, name: str, seconds: float, *, frequency=VOICE_HZ) -> str:
    key = tts_cache_key(hashlib.sha256(name.encode()).hexdigest())
    store.put_bytes(key, make_wav(seconds, frequency=frequency, amplitude=0.5))
    return key


def build(store, placements, *, music_seconds=VIDEO_MS / 1000, silent_music=False):
    """`placements`: (start_ms, clip seconds, stretch). Returns what the activity
    takes: the script ref, the segments key and the ingest result."""
    segments, doc_segments = [], []
    for i, (start, seconds, stretch) in enumerate(placements):
        segments.append(segment(i, f"câu {i}", slot_ms=3000, start_ms=start))
        doc_segments.append(
            DubSegment(
                id=i,
                text_vi=f"câu {i}",
                tts_key=put_clip(store, f"clip-{i}", seconds),
                duration_ms=round(seconds * 1000),
                stretch_ratio=stretch,
                overflow_unresolved=False,
            )
        )
    script_key = put_script(store, segments, version=2)
    segments_key = work_key("7001", 0, "dub-segments.json")
    document = DubSegmentsDocument(aweme_id="7001", script_version=2, segments=tuple(doc_segments))
    store.put_bytes(segments_key, document.model_dump_json().encode())
    ref = LocalizedScriptRef(script_key=script_key, version=2)
    return ref, segments_key, ingest(store, seconds=music_seconds, silent_music=silent_music)


def activity(store, goal=LOUDNESS) -> AssembleAudioActivity:
    return AssembleAudioActivity(store, loudness_lufs=goal, true_peak_dbtp=PEAK)


async def assemble(store, placements, *, goal=LOUDNESS, **kw):
    ref, segments_key, prep = build(store, placements, **kw)
    result = await ActivityEnvironment().run(
        activity(store, goal).assemble_audio, SPEC, ref, segments_key, prep, 0
    )
    path = Path(tempfile.mkdtemp()) / "dubbed.wav"
    store.download_file(result.audio_key, path, produced_by="dub")
    return result, path


# --- the filter graph -----------------------------------------------------------


def test_the_graph_places_each_clip_at_its_start_and_stretch() -> None:
    graph = build_filter_graph([(2000, 1.0), (5300, 1.15)], 8000)
    assert "[1:a]" in graph and "adelay=2000:all=1" in graph and "atempo=1.000000" in graph
    assert "[2:a]" in graph and "adelay=5300:all=1" in graph and "atempo=1.150000" in graph
    assert "sidechaincompress" in graph
    music = next(line for line in graph.splitlines() if line.startswith("[0:a]"))
    assert "apad=whole_dur=8.000" in music and "atrim=end=8.000" in music


def test_a_track_with_no_clips_is_just_the_music() -> None:
    graph = build_filter_graph([], 4000)
    assert "sidechaincompress" not in graph
    assert "[mix]" in graph


def test_the_loudness_report_is_read_from_the_end_of_the_output() -> None:
    noisy = b'warning {not json}\n{"input_i": "-20.5", "input_tp": "-3.0"}\nbye'
    assert parse_loudnorm(noisy)["input_i"] == "-20.5"
    with pytest.raises(PermanentProviderError):
        parse_loudnorm(b"nothing useful")


# --- the track ------------------------------------------------------------------


async def test_the_track_is_as_long_as_the_video_and_its_hash_is_of_the_stored_file(
    store: ObjectStore,
) -> None:
    result, path = await assemble(store, [(1000, 1.5, 1.0), (4000, 1.0, 1.0)])
    assert result.audio_key == work_key("7001", 0, "dubbed.wav")
    samples = decode(path)
    assert len(samples) * 1000 / RATE == pytest.approx(VIDEO_MS, abs=50)
    stored = store.get_bytes(result.audio_key, produced_by="dub")
    assert result.audio_sha256 == hashlib.sha256(stored).hexdigest()
    assert result.script_version == 2
    assert result.script_key == work_key("7001", 0, "script.v2.json")


@pytest.mark.parametrize("goal", [-14.0, -16.0])
async def test_the_loudness_is_within_one_lu_of_the_goal(store: ObjectStore, goal: float) -> None:
    _, path = await assemble(store, [(1000, 2.0, 1.0), (4000, 2.0, 1.0)], goal=goal)
    assert integrated_lufs(path) == pytest.approx(goal, abs=1.0)


async def test_a_clip_starts_where_its_segment_starts(store: ObjectStore) -> None:
    _, path = await assemble(store, [(2000, 1.5, 1.0)])
    span = tone_span_ms(decode(path), VOICE_HZ)
    assert span is not None
    assert span[0] == pytest.approx(2000, abs=60)
    assert span[1] == pytest.approx(3500, abs=60)


async def test_a_clip_is_played_at_its_stretch_ratio(store: ObjectStore) -> None:
    """Two seconds of audio sped up 1.15x take 1.74 s; slowed to 0.85x, 2.35 s."""
    _, path = await assemble(store, [(2000, 2.0, 1.15)])
    span = tone_span_ms(decode(path), VOICE_HZ)
    assert span is not None
    assert span[1] - span[0] == pytest.approx(1739, abs=80)
    _, slow = await assemble(store, [(2000, 2.0, 0.85)])
    span = tone_span_ms(decode(slow), VOICE_HZ)
    assert span is not None
    assert span[1] - span[0] == pytest.approx(2353, abs=80)


async def test_clips_are_placed_independently_of_each_other(store: ObjectStore) -> None:
    _, path = await assemble(store, [(500, 1.0, 1.0), (5000, 1.0, 1.0)])
    samples = decode(path)
    assert level(samples, 600, 1400, VOICE_HZ) > 0.01
    assert level(samples, 5100, 5900, VOICE_HZ) > 0.01
    assert level(samples, 2500, 4500, VOICE_HZ) < 0.005


async def test_the_music_is_ducked_while_the_voice_speaks(store: ObjectStore) -> None:
    _, path = await assemble(store, [(3000, 2.0, 1.0)])
    samples = decode(path)
    under_voice = level(samples, 3300, 4800, MUSIC_HZ)
    alone = level(samples, 5500, 7000, MUSIC_HZ)
    assert alone > 0
    assert db(under_voice / alone) < -6


async def test_music_runs_at_full_level_where_nobody_speaks(store: ObjectStore) -> None:
    """Control for the ducking test: the same music, away from the voice, is not lowered."""
    _, path = await assemble(store, [(5000, 1.0, 1.0)])
    samples = decode(path)
    early = level(samples, 500, 2000, MUSIC_HZ)
    late = level(samples, 6500, 7900, MUSIC_HZ)
    assert abs(db(early / late)) < 2


async def test_music_longer_than_the_video_is_cut_to_it(store: ObjectStore) -> None:
    _, path = await assemble(store, [(1000, 1.0, 1.0)], music_seconds=VIDEO_MS / 1000 + 0.4)
    assert len(decode(path)) * 1000 / RATE == pytest.approx(VIDEO_MS, abs=50)


async def test_music_shorter_than_the_video_is_padded_to_it(store: ObjectStore) -> None:
    _, path = await assemble(store, [(1000, 1.0, 1.0)], music_seconds=VIDEO_MS / 1000 - 0.3)
    assert len(decode(path)) * 1000 / RATE == pytest.approx(VIDEO_MS, abs=50)


async def test_music_shorter_than_the_video_is_padded_even_when_nobody_speaks(
    store: ObjectStore,
) -> None:
    _, path = await assemble(store, [], music_seconds=VIDEO_MS / 1000 - 0.3)
    assert len(decode(path)) * 1000 / RATE == pytest.approx(VIDEO_MS, abs=50)


async def test_a_voice_that_runs_past_the_end_of_the_video_fails_for_good(
    store: ObjectStore,
) -> None:
    with pytest.raises(ApplicationError) as failure:
        await assemble(store, [(7000, 2.0, 1.0)])
    assert failure.value.type == "AudioLengthMismatch"
    assert failure.value.non_retryable
    assert not store.exists(work_key("7001", 0, "dubbed.wav"))


async def test_a_silent_track_fails_for_good_and_stores_nothing(store: ObjectStore) -> None:
    ref, segments_key, prep = build(store, [], silent_music=True)
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(
            activity(store).assemble_audio, SPEC, ref, segments_key, prep, 0
        )
    assert failure.value.type == "SilentAudio"
    assert not store.exists(work_key("7001", 0, "dubbed.wav"))


async def test_segments_made_for_another_script_version_are_refused(store: ObjectStore) -> None:
    ref, segments_key, prep = build(store, [(1000, 1.0, 1.0)])
    stale = ref.model_copy(update={"version": 3})
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(
            activity(store).assemble_audio, SPEC, stale, segments_key, prep, 0
        )
    assert failure.value.type == "ScriptVersionMismatch"


async def test_the_round_names_the_folder_the_track_is_written_to(store: ObjectStore) -> None:
    ref, segments_key, prep = build(store, [(1000, 1.0, 1.0)])
    result = await ActivityEnvironment().run(
        activity(store).assemble_audio, SPEC, ref, segments_key, prep, 2
    )
    assert result.audio_key == work_key("7001", 2, "dubbed.wav")
    assert store.exists(result.audio_key)
