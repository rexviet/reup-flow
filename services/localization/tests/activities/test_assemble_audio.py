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
    check_length,
    parse_loudnorm,
)
from localization.activities.script_document import read_script
from localization.contracts import (
    DubSegment,
    DubSegmentsDocument,
    IngestResult,
    LocalizedScriptRef,
)
from localization.providers.errors import PermanentProviderError
from localization.storage.object_store import ObjectStore, tts_cache_key, work_key
from tests.support.audio import (
    RATE,
    db,
    decode,
    integrated_lufs,
    level,
    tone_span_ms,
    true_peak_dbtp,
)
from tests.support.inputs import make_input
from tests.support.tts_fakes import make_clicks, make_wav, put_script, segment

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


def put_clip(store: ObjectStore, name: str, seconds: float, *, spiky=False) -> str:
    key = tts_cache_key(hashlib.sha256(name.encode()).hexdigest())
    wav = make_clicks(seconds) if spiky else make_wav(seconds, frequency=VOICE_HZ, amplitude=0.5)
    store.put_bytes(key, wav)
    return key


def build(store, placements, *, music_seconds=VIDEO_MS / 1000, silent_music=False, spiky=False):
    """`placements`: (start_ms, clip seconds, stretch). Returns what the activity
    takes: the script ref, the segments key and the ingest result."""
    segments, doc_segments = [], []
    for i, (start, seconds, stretch) in enumerate(placements):
        segments.append(segment(i, f"câu {i}", slot_ms=3000, start_ms=start))
        doc_segments.append(
            DubSegment(
                id=i,
                text_vi=f"câu {i}",
                tts_key=put_clip(store, f"clip-{i}", seconds, spiky=spiky),
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
    voice = next(line for line in graph.splitlines() if line.rstrip(";").endswith("[voice]"))
    assert "apad=whole_dur=8.000" in voice
    final = next(line for line in graph.splitlines() if line.rstrip(";").endswith("[mix]"))
    assert "atrim=end=8.000" in final
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


async def test_a_peaky_voice_stays_under_the_true_peak_ceiling(store: ObjectStore) -> None:
    """Loud clicks reach the loudness goal with a peak far above the ceiling unless
    the ceiling is applied: the configured TRUE_PEAK_DBTP is what holds it down."""
    _, path = await assemble(store, [(1000, 2.0, 1.0), (4000, 2.0, 1.0)], spiky=True)
    assert integrated_lufs(path) == pytest.approx(LOUDNESS, abs=1.0)
    assert true_peak_dbtp(path) <= PEAK + 0.3


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


async def test_a_voice_that_runs_past_the_end_of_the_video_is_cut_there(
    store: ObjectStore,
) -> None:
    """A line that still overflows at the largest stretch is marked and plays; its
    tail is lost at the end of the video. The run must not fail after the speech is
    paid for: 3 s of voice from 7 s, sped up 1.15x, would end at 9.6 s on an 8 s video."""
    result, path = await assemble(store, [(7000, 3.0, 1.15)])
    samples = decode(path)
    assert len(samples) * 1000 / RATE == pytest.approx(VIDEO_MS, abs=50)
    span = tone_span_ms(samples, VOICE_HZ)
    assert span is not None
    assert span[0] == pytest.approx(7000, abs=60)
    assert span[1] == pytest.approx(VIDEO_MS, abs=60)
    assert store.exists(result.audio_key)


def test_a_track_of_the_wrong_length_is_refused_for_good(tmp_path: Path) -> None:
    """With the voice cut to the video, a wrong length can only be a bug in the
    graph; the check is what turns it into a failure and not a drifting track."""
    path = tmp_path / "t.wav"
    path.write_bytes(make_wav(VIDEO_MS / 1000 + 0.5))
    with pytest.raises(ApplicationError) as failure:
        check_length(path, VIDEO_MS)
    assert failure.value.type == "AudioLengthMismatch"
    assert failure.value.non_retryable
    check_length(path, VIDEO_MS + 500)


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


@pytest.mark.parametrize("background", ["music", "silent", "missing"])
@pytest.mark.parametrize("mixed", [False, True])
async def test_intentional_omissions_use_only_music_or_silence(store, background, mixed, tmp_path):
    # Give the original vocal a loud tone: reading it would be audible and detected.
    ref, key, prep = build(store, [(1000, 1.0, 1.0)] if mixed else [])
    script = read_script(store, ref.script_key)
    omitted = segment(9, "", start_ms=4000).model_copy(
        update={"status": "unresolved", "reason": "No recoverable speech"}
    )
    put_script(store, [*script.segments, omitted], version=ref.version)
    doc = DubSegmentsDocument.model_validate_json(store.get_bytes(key, produced_by="test"))
    doc = doc.model_copy(
        update={
            "segments": (
                *doc.segments,
                DubSegment(
                    id=9,
                    text_vi="",
                    status="unresolved",
                    reason=omitted.reason,
                    duration_ms=0,
                    stretch_ratio=1.0,
                    overflow_unresolved=False,
                ),
            )
        }
    )
    store.put_bytes(key, doc.model_dump_json().encode())
    if background == "silent":
        store.put_bytes(prep.bgm_key, make_wav(VIDEO_MS / 1000, amplitude=0))
    elif background == "missing":
        prep = prep.model_copy(update={"bgm_key": work_key("7001", 0, "missing-bgm.wav")})
    store.put_bytes(prep.vocals_key, make_wav(VIDEO_MS / 1000, frequency=2100, amplitude=0.8))
    result = await ActivityEnvironment().run(
        activity(store).assemble_audio, SPEC, ref, key, prep, 0
    )
    path = tmp_path / "out.wav"
    store.download_file(result.audio_key, path, produced_by="test")
    check_length(path, VIDEO_MS)
    samples = decode(path)
    # At the omitted slot there must be no vocal tone, regardless of BGM availability.
    assert level(samples, 4000, 5000, 2100) < 0.001
    if background != "music" and not mixed:
        assert max(abs(v) for v in samples) == 0


# --- sung segments keep the original sound ------------------------------------------------

SINGING_HZ = 2100


def test_the_graph_lays_the_original_vocals_over_each_kept_span_unducked() -> None:
    graph = build_filter_graph([(500, 1.0)], 8000, [(2000, 4000), (6000, 7000)])
    lines = graph.splitlines()
    # The vocals are the input after the clips, cut to each span, faded in and out there.
    assert any(line.startswith("[2:a]") and "asplit=2[o0][o1]" in line for line in lines)
    first = next(line for line in lines if line.startswith("[o0]"))
    assert "atrim=start=2.000:end=4.000" in first and "adelay=2000:all=1" in first
    assert "afade=t=in:d=0.060" in first and "afade=t=out:st=1.940:d=0.060" in first
    # Only the dub keys the ducker; the kept vocals join the final mix.
    duck = next(line for line in lines if "sidechaincompress" in line)
    assert duck.startswith("[bgm][vkey]")
    final = next(line for line in lines if line.rstrip(";").endswith("[mix]"))
    assert final.startswith("[ducked][vmix][keep]amix=inputs=3")


def test_a_track_of_only_sung_segments_is_the_music_and_the_vocals() -> None:
    graph = build_filter_graph([], 4000, [(0, 4000)])
    assert "sidechaincompress" not in graph
    final = next(line for line in graph.splitlines() if line.rstrip(";").endswith("[mix]"))
    assert final.startswith("[bgm][keep]amix=inputs=2")


def test_without_kept_spans_the_graph_is_what_it_was() -> None:
    assert build_filter_graph([(2000, 1.0)], 8000, []) == build_filter_graph([(2000, 1.0)], 8000)


async def test_a_sung_segment_keeps_the_original_vocals_and_the_music_unducked(store) -> None:
    ref, key, prep = build(store, [(500, 1.0, 1.0)])
    script = read_script(store, ref.script_key)
    sung = segment(5, "lời bài hát", start_ms=3000, slot_ms=3000).model_copy(
        update={"keep_original": True}
    )
    put_script(store, [*script.segments, sung], version=ref.version)
    doc = DubSegmentsDocument.model_validate_json(store.get_bytes(key, produced_by="test"))
    kept = DubSegment(
        id=5,
        text_vi="lời bài hát",
        duration_ms=0,
        stretch_ratio=1.0,
        overflow_unresolved=False,
        keep_original=True,
    )
    store.put_bytes(
        key, doc.model_copy(update={"segments": (*doc.segments, kept)}).model_dump_json().encode()
    )
    store.put_bytes(prep.vocals_key, make_wav(VIDEO_MS / 1000, frequency=SINGING_HZ, amplitude=0.5))
    result = await ActivityEnvironment().run(
        activity(store).assemble_audio, SPEC, ref, key, prep, 0
    )
    path = Path(tempfile.mkdtemp()) / "out.wav"
    store.download_file(result.audio_key, path, produced_by="test")
    check_length(path, VIDEO_MS)
    samples = decode(path)
    # The original vocals sound over the sung span, and only there.
    span = tone_span_ms(samples, SINGING_HZ)
    assert span is not None
    assert span[0] == pytest.approx(3000, abs=80) and span[1] == pytest.approx(6000, abs=80)
    # Nothing ducks the music under the singing: it is as loud as where nobody speaks.
    assert abs(db(level(samples, 3300, 5700, MUSIC_HZ) / level(samples, 6300, 7900, MUSIC_HZ))) < 2


def test_original_voice_runs_quietly_under_the_dub_and_not_over_sung_spans():
    graph = build_filter_graph([(1000, 1.0)], 10_000, [(4000, 6000)], under=0.3)
    # One decoded vocals stream feeds both the sung spans and the quiet layer.
    assert "[2:a]" in graph and "asplit=2[vk][vu]" in graph
    assert "[vu]volume=0.3000,volume=0:enable='between(t,4.000,6.000)'[under]" in graph
    # The quiet layer is mixed after the ducker: it neither keys it nor is ducked.
    assert "[ducked][vmix][keep][under]amix=inputs=4" in graph
    assert "[under]" not in graph.split("sidechaincompress")[0].split("[voice]")[-1]


def test_quiet_original_voice_without_sung_spans_or_clips():
    only_dub = build_filter_graph([(0, 1.0)], 5_000, None, under=0.3)
    assert "[2:a]" in only_dub and "[ducked][vmix][under]amix=inputs=3" in only_dub
    none_spoken = build_filter_graph([], 5_000, None, under=0.3)
    assert "[bgm][under]amix=inputs=2" in none_spoken
    assert "under" not in build_filter_graph([(0, 1.0)], 5_000, None)
