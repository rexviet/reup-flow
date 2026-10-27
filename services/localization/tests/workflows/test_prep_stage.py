"""The preparation stages: the workflow around them, and each activity."""

import asyncio
import dataclasses
import json
import subprocess
import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.detect_subtitles import (
    BATCH_SIZE,
    DetectSubtitlesActivity,
    _consensus,
    locate,
    to_source,
)
from localization.activities.ffmpeg import parse_probe
from localization.activities.ingest import IngestActivity
from localization.activities.persist import PersistActivity
from localization.activities.transcribe import SILENCE_GAP_MS, TranscribeActivity, regroup
from localization.config import Settings
from localization.contracts import (
    BBox,
    LocalizationInput,
    LocalizationResult,
    PublishRequest,
    PublishResult,
    SttWord,
    SubtitleTrack,
    TranscriptDocument,
)
from localization.providers.errors import PermanentProviderError, UnusableAnswer
from localization.storage.object_store import ArtifactMissingError, ObjectStore, work_key
from localization.workers.run import QueueSpec, build_worker, queue_specs
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.support.inputs import make_input
from tests.support.prep_fakes import REGION, PrepFakes
from tests.support.prep_worker import fake_activities, prep_workers, running

BUCKET = "reup-media"


# --- the workflow --------------------------------------------------------------


async def _run(client: Client, fakes: PrepFakes):
    """Runs the real workflow on four real task queues, like production."""
    async with prep_workers(client, fakes):
        handle = await client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        try:
            return handle, await asyncio.wait_for(handle.result(), timeout=10)
        except Exception as exc:  # noqa: BLE001
            return handle, exc


async def _scheduled(handle) -> list[tuple[str, str]]:
    history = await handle.fetch_history()
    return [
        (
            e.activity_task_scheduled_event_attributes.activity_type.name,
            e.activity_task_scheduled_event_attributes.task_queue.name,
        )
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


async def test_preparation_runs_each_stage_once_on_its_own_queue(temporal_client: Client) -> None:
    fakes = PrepFakes(delay_seconds=0.5)
    handle, result = await _run(temporal_client, fakes)
    assert isinstance(result, LocalizationResult), result
    assert result.prep.subtitles.region == REGION
    assert result.prep.transcript.segment_count == 2
    assert dict(fakes.calls) == {"ingest": 1, "transcribe": 1, "detect_subtitles": 1}
    # The preparation stages come first, in order, each on its own queue (the voice's
    # rate is measured beside the ingest); what follows is the Localizer stage (tests/agents).
    assert (await _scheduled(handle))[:4] == [
        ("ingest", "media"),
        ("voice_rate", "tts"),
        ("transcribe", "stt"),
        ("detect_subtitles", "io"),
    ]


async def test_transcribe_and_detect_run_at_the_same_time(temporal_client: Client) -> None:
    fakes = PrepFakes(delay_seconds=0.8)
    _, result = await _run(temporal_client, fakes)
    assert isinstance(result, LocalizationResult), result
    spans = {name: (start, end) for name, start, end in fakes.spans}
    (t_start, t_end), (d_start, d_end) = spans["transcribe"], spans["detect_subtitles"]
    assert max(t_start, d_start) < min(t_end, d_end), "2a and 2b did not overlap"


async def test_a_lost_artifact_reruns_the_stage_that_made_it_exactly_once(
    temporal_client: Client,
) -> None:
    fakes = PrepFakes(lose_vocals_on_ingest_runs={1})
    handle, result = await _run(temporal_client, fakes)
    assert isinstance(result, LocalizationResult), result
    assert fakes.calls["ingest"] == 2
    scheduled = [name for name, _ in await _scheduled(handle)]
    assert scheduled.count("ingest") == 2
    assert scheduled.count("transcribe") == 2  # the failed one, then the rerun


async def test_a_second_loss_fails_the_workflow_instead_of_looping(
    temporal_client: Client,
) -> None:
    fakes = PrepFakes(lose_vocals_on_ingest_runs={1, 2, 3, 4})
    _, outcome = await _run(temporal_client, fakes)
    assert isinstance(outcome, WorkflowFailureError), outcome
    assert fakes.calls["ingest"] == 2, "the stage must be re-run exactly once"


@activity.defn(name="publish")
async def record_outcome(spec: LocalizationInput, request: PublishRequest) -> PublishResult:
    """A run that fails is written on the run by `publish`: a stand-in that takes the note."""
    return PublishResult(outcome=request.outcome)


async def test_a_missing_source_video_is_not_regenerated(temporal_client: Client) -> None:
    """The workflow cannot re-run the downloader, so a lost source fails the run
    without re-running `ingest`."""
    fakes = PrepFakes()
    acts = fake_activities(fakes)

    @activity.defn(name="detect_subtitles")
    async def detect(spec: LocalizationInput) -> SubtitleTrack:
        raise ArtifactMissingError(spec.video_key, "download")

    workers = [
        build_worker(
            temporal_client, QueueSpec("workflows", workflows=[VideoLocalizationWorkflow])
        ),
        build_worker(temporal_client, QueueSpec("media", activities=[acts["ingest"]])),
        build_worker(temporal_client, QueueSpec("stt", activities=[acts["transcribe"]])),
        build_worker(temporal_client, QueueSpec("io", activities=[detect, record_outcome])),
    ]
    async with running(workers):
        handle = await temporal_client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
        )
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=30)
    assert fakes.calls["ingest"] == 1


def test_the_workflow_names_only_activities_that_are_registered() -> None:
    """The workflow calls activities by string. A typo would only surface as a
    run that never finishes, so pin the names against the real registrations."""
    store = ObjectStore(object(), BUCKET)
    specs = queue_specs("agents", Settings(), store, _providers()) + queue_specs(
        "media", Settings(), store, _providers()
    )
    registered = {
        fn.__temporal_activity_definition.name  # type: ignore[attr-defined]
        for spec in specs
        for fn in spec.activities
    }
    source = Path("src/localization/workflows/video_localization.py").read_text()
    for name in ("ingest", "transcribe", "detect_subtitles", "voice_rate", "assemble_script"):
        assert f'"{name}"' in source
        assert name in registered


def _providers():  # noqa: ANN202
    from types import SimpleNamespace

    return SimpleNamespace(stt=object(), ocr=object(), tts=object(), demucs=object())


# --- ffprobe --------------------------------------------------------------------


def test_a_file_with_only_cover_art_has_no_video_stream() -> None:
    body = {
        "streams": [
            {"codec_type": "audio"},
            {
                "codec_type": "video",
                "width": 500,
                "height": 500,
                "disposition": {"attached_pic": 1},
            },
        ],
        "format": {"duration": "10"},
    }
    with pytest.raises(PermanentProviderError, match="no_video_stream"):
        parse_probe(body)


def test_probe_reads_size_rate_duration_and_audio() -> None:
    info = parse_probe(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1080,
                    "height": 1920,
                    "avg_frame_rate": "30000/1001",
                    "disposition": {"attached_pic": 0},
                },
                {"codec_type": "audio"},
            ],
            "format": {"duration": "12.345"},
        }
    )
    assert (info.width, info.height, info.fps, info.duration_ms, info.has_audio) == (
        1080,
        1920,
        30,
        12345,
        True,
    )


# --- activities on real media ---------------------------------------------------


@pytest.fixture
def store():  # noqa: ANN201
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield ObjectStore(client, BUCKET)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args], check=True)


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "v.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-shortest", "-pix_fmt", "yuv420p", str(path),
    )  # fmt: skip
    return path


class FakeSeparator:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.inputs: list[Path] = []
        self.durations: list[int] = []
        self.work_dir: Path | None = None

    def separate(
        self, input_path: Path, duration_ms: int = 0, cancel: object | None = None
    ) -> tuple[Path, Path, Path]:
        self.inputs.append(input_path)
        self.durations.append(duration_ms)
        self.work_dir = self.tmp / f"stems-{uuid.uuid4().hex[:6]}"
        self.work_dir.mkdir()
        vocals, bgm = self.work_dir / "vocals.wav", self.work_dir / "no_vocals.wav"
        vocals.write_bytes(b"RIFF-vocals")
        bgm.write_bytes(b"RIFF-bgm")
        return self.work_dir, vocals, bgm


async def test_ingest_stores_vocals_and_background_and_reports_the_video(
    store: ObjectStore, video: Path, tmp_path: Path
) -> None:
    spec = make_input()
    store.put_file(spec.video_key, video)
    separator = FakeSeparator(tmp_path)
    result = await ActivityEnvironment().run(IngestActivity(store, separator).ingest, spec)
    assert result.vocals_key == work_key("7001", 0, "vocals.wav")
    assert result.bgm_key == work_key("7001", 0, "bgm.wav")
    assert store.get_bytes(result.vocals_key, produced_by="ingest") == b"RIFF-vocals"
    assert store.get_bytes(result.bgm_key, produced_by="ingest") == b"RIFF-bgm"
    assert (result.width, result.height, result.fps) == (320, 240, 10)
    assert 2900 <= result.duration_ms <= 3100
    assert separator.inputs[0].suffix == ".wav"
    assert separator.durations == [result.duration_ms]  # the timeout follows the media
    assert separator.work_dir is not None and not separator.work_dir.exists()


async def test_ingest_without_a_video_stream_is_a_permanent_failure(
    store: ObjectStore, tmp_path: Path
) -> None:
    mp3 = tmp_path / "a.mp3"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(mp3))
    spec = make_input()
    store.put_file(spec.video_key, mp3)
    separator = FakeSeparator(tmp_path)
    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(IngestActivity(store, separator).ingest, spec)
    assert caught.value.non_retryable is True
    assert "no_video_stream" in str(caught.value)
    assert separator.inputs == []


async def test_ingest_of_a_lost_source_names_the_downloader_as_its_producer(
    store: ObjectStore, tmp_path: Path
) -> None:
    with pytest.raises(ArtifactMissingError) as caught:
        await ActivityEnvironment().run(
            IngestActivity(store, FakeSeparator(tmp_path)).ingest, make_input()
        )
    assert caught.value.stage == "download"


def _doc(words: list[tuple[str, int, int]]) -> TranscriptDocument:
    return TranscriptDocument(
        language="zh",
        duration_ms=10_000,
        segments=(),
        words=tuple(SttWord(text=t, start_ms=s, end_ms=e) for t, s, e in words),
    )


def test_a_pause_over_400ms_ends_a_segment_and_exactly_400ms_does_not() -> None:
    assert SILENCE_GAP_MS == 400
    grouped = regroup(_doc([("你", 0, 300), ("好", 700, 900), ("世", 1301, 1500)]))
    assert [(s.start_ms, s.end_ms, s.text) for s in grouped.segments] == [
        (0, 900, "你好"),
        (1301, 1500, "世"),
    ]
    assert [s.id for s in grouped.segments] == [0, 1]


def test_latin_words_are_spaced_and_chinese_words_are_not() -> None:
    grouped = regroup(
        _doc([("买", 0, 100), ("iPhone", 100, 300), ("15", 300, 400), ("吧", 400, 500)])
    )
    assert [s.text for s in grouped.segments] == ["买iPhone 15吧"]


def test_without_word_timestamps_the_providers_segments_are_kept() -> None:
    doc = TranscriptDocument(language="zh", duration_ms=1, segments=(), words=())
    assert regroup(doc) is doc


class FakeStt:
    def __init__(self, doc: TranscriptDocument) -> None:
        self.doc = doc
        self.audio: list[bytes] = []

    async def transcribe(self, audio: bytes, *, filename: str = "", language: str | None = None):  # noqa: ANN201
        self.audio.append(audio)
        return self.doc


def _stem(tmp_path: Path) -> bytes:
    """A 44.1 kHz stereo WAV, like a Demucs stem."""
    path = tmp_path / "stem.wav"
    _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-ac", "2", "-ar", "44100", str(path)
    )
    return path.read_bytes()


def _audio_format(wav: bytes, tmp_path: Path) -> tuple[int, int]:
    path = tmp_path / "probe.wav"
    path.write_bytes(wav)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate,channels", "-of", "json", str(path)],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    stream = json.loads(out)["streams"][0]
    return int(stream["sample_rate"]), int(stream["channels"])


async def test_transcribe_stores_the_regrouped_transcript(
    store: ObjectStore, tmp_path: Path
) -> None:
    spec = make_input()
    vocals = work_key("7001", 0, "vocals.wav")
    store.put_bytes(vocals, _stem(tmp_path))
    stt = FakeStt(_doc([("你", 0, 300), ("好", 900, 1000)]))
    result = await ActivityEnvironment().run(
        TranscribeActivity(store, stt).transcribe, spec, vocals
    )
    assert result.segment_count == 2
    stored = json.loads(store.get_bytes(result.transcript_key, produced_by="transcribe"))
    assert [s["text"] for s in stored["segments"]] == ["你", "好"]
    assert result.transcript_key == work_key("7001", 0, "transcript.json")


async def test_stt_gets_16khz_mono_not_the_44khz_stereo_stem(
    store: ObjectStore, tmp_path: Path
) -> None:
    vocals = work_key("7001", 0, "vocals.wav")
    stem = _stem(tmp_path)
    store.put_bytes(vocals, stem)
    stt = FakeStt(_doc([]))
    await ActivityEnvironment().run(TranscribeActivity(store, stt).transcribe, make_input(), vocals)
    assert _audio_format(stem, tmp_path) == (44100, 2)
    assert _audio_format(stt.audio[0], tmp_path) == (16000, 1)
    assert len(stt.audio[0]) < len(stem) / 2


async def test_transcribe_of_lost_vocals_names_ingest_as_its_producer(store: ObjectStore) -> None:
    stt = FakeStt(_doc([]))
    with pytest.raises(ArtifactMissingError) as caught:
        await ActivityEnvironment().run(
            TranscribeActivity(store, stt).transcribe,
            make_input(),
            work_key("7001", 0, "vocals.wav"),
        )
    assert caught.value.stage == "ingest"
    assert stt.audio == []


def _box(y: int, h: int = 60, x: int = 100, w: int = 800) -> BBox:
    return BBox(x=x, y=y, w=w, h=h)


def test_locate_picks_the_most_common_line_and_builds_cues() -> None:
    low, high = _box(1700), _box(100)
    #        0     1    2    3     4     5     6     7     8     9    10
    boxes = [None, low, low, high, None, None, None, None, None, low, low]
    region, cues = locate(boxes, 1080, 1920, 5500)
    assert region is not None
    assert region.y <= 1700 and region.y + region.h >= 1760  # the low line, not the top one
    assert [(c.start_ms, c.end_ms) for c in cues] == [(500, 1500), (4500, 5500)]


def test_a_gap_is_bridged_by_its_length_in_time_not_by_a_number_of_frames() -> None:
    low = _box(1700)
    four_missed = [low, None, None, None, None, low]  # 2000 ms without a detection
    five_missed = [low, None, None, None, None, None, low]  # 2500 ms
    assert len(locate(four_missed, 1080, 1920, 0)[1]) == 1
    assert len(locate(five_missed, 1080, 1920, 0)[1]) == 2


def test_a_single_detection_is_not_a_subtitle_and_none_means_no_region() -> None:
    assert locate([None, _box(1700), None], 1080, 1920, 0) == (None, ())
    assert locate([None, None], 1080, 1920, 0) == (None, ())


# --- the real clip, as plain data ---------------------------------------------------------------
# The model's boxes for the 12 frames of a real 1080x1920 clip that show a subtitle, in
# the 576x1024 frame it saw (decoded as xyxy_norm1000): (top, bottom, left, right). The
# true text is y 691-732 there (x 200-860, y 1296-1380 in source pixels). Most boxes are
# right to within about 10 px; f1 and f4 are other text, and f11 is far above the line.
REAL_CLIP = [
    (860, 924, 120, 493),  # f1: another text, an outlier
    (691, 740, 109, 449),
    (707, 762, 91, 476),
    (770, 852, 166, 409),  # f4: an outlier
    (680, 735, 102, 472),
    (690, 732, 114, 455),
    (688, 731, 144, 432),
    (666, 725, 101, 473),
    (676, 728, 170, 405),
    (690, 731, 156, 422),
    (645, 706, 160, 415),  # f11: 45 px above the text
    (688, 727, 166, 410),
]
SCALED, SOURCE = (576, 1024), (1080, 1920)


def _real_clip() -> list[BBox | None]:
    boxes: list[BBox | None] = [
        to_source(BBox(x=left, y=top, w=right - left, h=bottom - top), SCALED, SOURCE)
        for top, bottom, left, right in REAL_CLIP
    ]
    return [*boxes, *([None] * 10)]  # frames 13-22 (after 6 s) were answered null


def test_the_real_clip_gives_one_cue_and_a_region_on_the_text_without_the_outliers() -> None:
    boxes = _real_clip()
    assert _consensus(boxes) == [1, 2, 4, 5, 6, 7, 8, 9, 11]  # not f1, f4 and f11
    region, cues = locate(boxes, 1080, 1920, 11_100)
    assert [(c.start_ms, c.end_ms) for c in cues] == [(500, 6000)]
    assert region is not None
    # Before the 13% padding (12 px) the vertical extent is the medians of the inliers.
    assert 1290 - 20 <= region.y + 12 <= 1290 + 20
    assert 1375 - 20 <= region.y + region.h - 12 <= 1375 + 20
    # Horizontally the widest inlier line (f3: 91-476 of 576, so 171-893 in source), padded.
    assert region.x <= 171 and region.x + region.w >= 893
    assert region.x + region.w <= 893 + 40


def test_the_real_clips_final_cover_hugs_the_text() -> None:
    from localization.activities.layout_captions import (
        caption_box_for,
        check_cover_plausible,
        grow_cover,
    )

    region, _ = locate(_real_clip(), 1080, 1920, 11_100)
    assert region is not None
    check_cover_plausible(region, 1080, 1920)  # raises ImplausibleCover when too large
    cover = caption_box_for(grow_cover(region, 1080, 1920), 1920)
    text = BBox(x=200, y=1296, w=660, h=84)
    assert cover.x <= text.x and cover.y <= text.y
    assert cover.x + cover.w >= text.x + text.w and cover.y + cover.h >= text.y + text.h
    assert cover.h <= 130  # the render that prompted this had a 219 px cover
    assert cover.w <= 0.8 * 1080


def test_the_region_is_padded_by_thirteen_percent_of_its_height_and_two_of_the_width() -> None:
    box = _box(1700, h=100, x=100, w=800)
    region, _ = locate([box, box], 1080, 1920, 0)
    assert region == BBox(x=78, y=1687, w=844, h=126)  # 13 px above and below, 22 px each side


def test_boxes_that_agree_on_no_row_give_no_region_and_no_crash() -> None:
    boxes = [_box(100), _box(500), _box(900), _box(1300), _box(1700)]
    assert locate(boxes, 1080, 1920, 0) == (None, ())


def test_two_equally_common_lines_give_the_lower_one() -> None:
    upper, lower = _box(1296, h=70), _box(1390, h=70)  # the two lines of a subtitle
    region, cues = locate([upper, lower, upper, lower, upper, lower], 1080, 1920, 0)
    assert region is not None
    assert region.y > 1296 + 70  # the lower line: ties go to the one lower on the screen
    # Only that line's frames make cues: every second frame, bridged across one miss.
    assert [(c.start_ms, c.end_ms) for c in cues] == [(500, 3000)]


def test_the_larger_of_two_lines_wins_whatever_its_position() -> None:
    upper, lower = _box(1296, h=70), _box(1390, h=70)
    region, _ = locate([upper, upper, upper, lower, lower], 1080, 1920, 0)
    assert region is not None and region.y + region.h <= 1296 + 70 + 20


def test_boxes_at_the_frame_edges_give_a_region_inside_the_frame() -> None:
    top, bottom = _box(0, h=40, x=0, w=1080), _box(1880, h=40, x=0, w=1080)
    for box in (top, bottom):
        region, _ = locate([box, box], 1080, 1920, 0)
        assert region is not None
        assert region.x >= 0 and region.y >= 0
        assert region.x + region.w <= 1080 and region.y + region.h <= 1920


class FakeOcr:
    def __init__(
        self,
        answers: dict[int, BBox],
        expect_size: tuple[int, int] = (320, 240),
        unusable: frozenset[int] = frozenset(),
        fail: Exception | None = None,
    ) -> None:
        self.answers = answers
        self.unusable = unusable
        self.fail = fail
        self.expect_size = expect_size
        self.calls = 0
        self.running = 0
        self.max_running = 0

    async def detect_region(self, frame_png: bytes, width: int, height: int) -> BBox | None:
        index = self.calls
        self.calls += 1
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        await asyncio.sleep(0.05)
        self.running -= 1
        assert frame_png[:2] == b"\xff\xd8"  # a JPEG, not a PNG
        assert (width, height) == self.expect_size
        if self.fail is not None:
            raise self.fail
        if index in self.unusable:
            raise UnusableAnswer("ocr: the reply is not the requested JSON")
        return self.answers.get(index)


async def test_detect_subtitles_samples_two_frames_a_second_in_batches_of_eight(
    store: ObjectStore, video: Path
) -> None:
    spec = make_input()
    store.put_file(spec.video_key, video)
    region = BBox(x=10, y=200, w=300, h=30)
    ocr = FakeOcr({i: region for i in range(1, 5)})
    result = await ActivityEnvironment().run(
        DetectSubtitlesActivity(store, ocr).detect_subtitles, spec
    )
    assert ocr.calls == 6  # 3 s at 2 frames per second
    assert 1 < ocr.max_running <= BATCH_SIZE
    assert result.region is not None  # fitted to the frames' pixels, see subtitle_pixels
    stored = json.loads(store.get_bytes(result.subtitle_key, produced_by="detect_subtitles"))
    assert stored["region"] == result.region.model_dump()
    assert stored["cues"] == [{"start_ms": 500, "end_ms": 2500}]


async def test_a_video_without_subtitles_has_no_region(store: ObjectStore, video: Path) -> None:
    spec = make_input()
    store.put_file(spec.video_key, video)
    result = await ActivityEnvironment().run(
        DetectSubtitlesActivity(store, FakeOcr({})).detect_subtitles, spec
    )
    assert result.region is None
    stored = json.loads(store.get_bytes(result.subtitle_key, produced_by="detect_subtitles"))
    assert stored["region"] is None and stored["cues"] == []


# --- persist_json -----------------------------------------------------------------


async def test_persist_json_writes_under_work_and_refuses_anywhere_else(
    store: ObjectStore,
) -> None:
    persist = PersistActivity(store).persist_json
    key = work_key("7001", 1, "script.json")
    await ActivityEnvironment().run(persist, key, {"b": 2, "a": "đ"})
    assert json.loads(store.get_bytes(key, produced_by="x")) == {"a": "đ", "b": 2}
    for bad in (
        "output/7001.mp4",
        "source/7001.mp4",
        "cache/tts/" + "a" * 64 + ".wav",
        work_key("7001", 0, "vocals.wav"),  # under work/, but not a JSON document
        "work/not-a-video/r0/x.json",
    ):
        with pytest.raises(ApplicationError) as caught:
            await ActivityEnvironment().run(persist, bad, {})
        assert caught.value.non_retryable is True
        assert not store.exists(bad)


def test_queue_limits_come_from_settings() -> None:
    settings = Settings(
        stt_concurrency=1,
        tts_concurrency=2,
        media_concurrency=3,
        llm_concurrency=5,
        io_concurrency=2,
    )
    store = ObjectStore(object(), BUCKET)
    agents = {s.name: s.limit for s in queue_specs("agents", settings, store, _providers())}
    media = {s.name: s.limit for s in queue_specs("media", settings, store, _providers())}
    assert agents == {"workflows": None, "llm": 5, "io": 2, "stt": 1, "tts": 2}
    assert media == {"media": 3}
    assert dataclasses.is_dataclass(QueueSpec)


# --- disk, scale and heartbeat ----------------------------------------------------------


@pytest.fixture
def long_video(tmp_path: Path) -> Path:
    path = tmp_path / "long.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=20",
        "-pix_fmt", "yuv420p", str(path),
    )  # fmt: skip
    return path


class DiskWatchingOcr(FakeOcr):
    """Counts the frame files on disk at the moment each frame is being read."""

    def __init__(self, root: Path) -> None:
        super().__init__({})
        self.root = root
        self.on_disk: list[int] = []

    async def detect_region(self, frame_png: bytes, width: int, height: int) -> BBox | None:
        self.on_disk.append(len(list(self.root.rglob("frame-*.jpg"))))
        return await super().detect_region(frame_png, width, height)


async def test_frames_on_disk_never_exceed_one_chunk_however_long_the_video(
    store: ObjectStore, long_video: Path, tmp_path: Path
) -> None:
    spec = make_input()
    store.put_file(spec.video_key, long_video)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ocr = DiskWatchingOcr(scratch)
    await ActivityEnvironment().run(
        DetectSubtitlesActivity(store, ocr, chunk_frames=8, tmp_root=scratch).detect_subtitles,
        spec,
    )
    assert ocr.calls == 40  # 20 s at 2 frames per second: five times the chunk
    assert max(ocr.on_disk) <= 8, ocr.on_disk
    assert max(ocr.on_disk) > 1, "the watcher saw no frames, so it proves nothing"
    assert list(scratch.iterdir()) == []  # and nothing is left behind


class SlowStore(ObjectStore):
    def put_file(self, key: str, path: Path, **kwargs) -> None:  # noqa: ANN003
        import time

        time.sleep(0.4)
        super().put_file(key, path, **kwargs)


async def test_ingest_heartbeats_while_the_stems_upload(
    store: ObjectStore, video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from localization.activities import ffmpeg

    monkeypatch.setattr(ffmpeg, "HEARTBEAT_SECONDS", 0.05)
    spec = make_input()
    store.put_file(spec.video_key, video)
    slow = SlowStore(store._client, BUCKET)
    beats: list[object] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    separator = FakeSeparator(tmp_path)
    # Uploads are the only slow step here, so every heartbeat after the separator
    # returned was sent while uploading.
    original = separator.separate

    def separate(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        result = original(*args, **kwargs)
        beats.clear()
        return result

    separator.separate = separate  # type: ignore[method-assign]
    await env.run(IngestActivity(slow, separator).ingest, spec)
    assert len(beats) >= 4, "no heartbeats while the stems were uploading"


async def test_a_container_longer_than_its_video_stream_does_not_fail_the_last_chunk(
    store: ObjectStore, tmp_path: Path
) -> None:
    """4 s of video is exactly one 8-frame chunk, and the 8 s audio track makes the
    container claim twice that: the next chunk seeks past the last frame, which
    ffmpeg reports as a failure, and that is just the end of the video."""
    path = tmp_path / "tail.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-pix_fmt", "yuv420p", str(path),
    )  # fmt: skip
    spec = make_input()
    store.put_file(spec.video_key, path)
    ocr = FakeOcr({})
    await ActivityEnvironment().run(
        DetectSubtitlesActivity(store, ocr, chunk_frames=8).detect_subtitles, spec
    )
    assert ocr.calls == 8


# --- a failed stage cancels its sibling ---------------------------------------------------


async def test_a_failed_stage_cancels_the_sibling_instead_of_leaving_it_running(
    temporal_client: Client,
) -> None:
    fakes = PrepFakes()
    acts = fake_activities(fakes)

    @activity.defn(name="transcribe")
    async def slow_transcribe(spec: LocalizationInput, vocals_key: str) -> None:
        await asyncio.sleep(4)

    @activity.defn(name="detect_subtitles")
    async def broken_detect(spec: LocalizationInput) -> SubtitleTrack:
        raise ApplicationError("cannot read the frames", non_retryable=True)

    workers = [
        build_worker(
            temporal_client, QueueSpec("workflows", workflows=[VideoLocalizationWorkflow])
        ),
        build_worker(temporal_client, QueueSpec("media", activities=[acts["ingest"]])),
        build_worker(temporal_client, QueueSpec("stt", activities=[slow_transcribe])),
        build_worker(temporal_client, QueueSpec("io", activities=[broken_detect, record_outcome])),
    ]
    async with running(workers):
        handle = await temporal_client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
        )
        started = asyncio.get_running_loop().time()
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=10)
        # It failed when detect failed, not when the 4 s transcription would end.
        assert asyncio.get_running_loop().time() - started < 3.5
        history = await handle.fetch_history()
    types = [e.event_type for e in history.events]
    assert EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED in types


# --- a malformed answer for one frame --------------------------------------------------------


@pytest.fixture
def ten_frames(tmp_path: Path) -> Path:
    path = tmp_path / "ten.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=5",
        "-pix_fmt", "yuv420p", str(path),
    )  # fmt: skip
    return path


async def _detect(store: ObjectStore, video: Path, ocr: FakeOcr):  # noqa: ANN202
    spec = make_input()
    store.put_file(spec.video_key, video)
    return await ActivityEnvironment().run(
        DetectSubtitlesActivity(store, ocr).detect_subtitles, spec
    )


async def test_one_unusable_answer_is_no_detection_for_that_frame_and_the_rest_still_count(
    store: ObjectStore, ten_frames: Path
) -> None:
    box = BBox(x=10, y=200, w=300, h=30)
    # Frame 4 is answered with prose; the frames around it show the subtitle.
    ocr = FakeOcr({i: box for i in (1, 2, 3, 5, 6)}, unusable=frozenset({4}))
    result = await _detect(store, ten_frames, ocr)
    assert result.region is not None
    stored = json.loads(store.get_bytes(result.subtitle_key, produced_by="detect_subtitles"))
    # The unusable frame is inside the gap that is bridged: one cue, not two.
    assert stored["cues"] == [{"start_ms": 500, "end_ms": 3500}]


async def test_most_frames_unusable_fails_permanently_and_names_the_counts(
    store: ObjectStore, ten_frames: Path
) -> None:
    ocr = FakeOcr({}, unusable=frozenset(range(7)))
    with pytest.raises(ApplicationError) as caught:
        await _detect(store, ten_frames, ocr)
    assert caught.value.non_retryable is True
    assert "7 of 10 sampled frames had an unusable answer" in str(caught.value)


async def test_a_few_unusable_answers_do_not_fail_the_activity(
    store: ObjectStore, ten_frames: Path
) -> None:
    box = BBox(x=10, y=200, w=300, h=30)
    ocr = FakeOcr({i: box for i in range(2, 9)}, unusable=frozenset({0, 9}))
    result = await _detect(store, ten_frames, ocr)
    assert result.region is not None


async def test_half_of_the_frames_unusable_is_not_more_than_half(
    store: ObjectStore, ten_frames: Path
) -> None:
    ocr = FakeOcr({}, unusable=frozenset(range(5)))
    assert (await _detect(store, ten_frames, ocr)).region is None


async def test_a_transport_failure_still_fails_the_activity_as_before(
    store: ObjectStore, ten_frames: Path
) -> None:
    from localization.providers.errors import TransientProviderError

    ocr = FakeOcr({}, fail=TransientProviderError("ocr: HTTP 503"))
    with pytest.raises(ApplicationError) as caught:
        await _detect(store, ten_frames, ocr)
    assert caught.value.non_retryable is False
    assert "503" in str(caught.value)
