"""The render: one mp4 per spec, keyed by the spec's hash, drawn by ffmpeg in one pass.

Every test that looks at the picture runs the real ffmpeg on a generated portrait video and
reads pixels back out of the output; nothing about ffmpeg is faked."""

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from PIL import Image
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.render import (
    RenderActivity,
    frame_at,
    frame_count,
    render_deadline_s,
    render_file,
)
from localization.config import DEFAULT_CAPTION_FONT
from localization.contracts import EditSpec, RenderInput, RenderResult
from localization.contracts.fixtures import samples
from localization.contracts.spec_hash import canonical_spec_json, spec_sha256
from localization.storage.object_store import ObjectStore, render_key, work_key
from tests.support.audio import decode, level

FONT = str(DEFAULT_CAPTION_FONT)
FONT_SHA = hashlib.sha256(Path(FONT).read_bytes()).hexdigest()
AWEME = "7001"
W, H, FPS = 1080, 1920, 30
SOURCE_GREY = 0x40
SOURCE_HZ = 1000
VOICE_HZ = 300
COVER = {"x": 40, "y": 1440, "w": 1000, "h": 240}
COVER_RGB = (0x20, 0x40, 0xC0)
COVER_HEX = "#2040c0"
COVER_PCT = 80
CAPTIONS = [
    {"segment_id": 0, "start_ms": 0, "end_ms": 1000, "text": "Xin chào các bạn", "font_px": 48},
    {
        "segment_id": 1,
        "start_ms": 1000,
        "end_ms": 2200,
        "text": "Hôm nay chúng ta học\ncách nấu một nồi phở",
        "font_px": 40,
    },
]
SPEC_KEY = work_key(AWEME, 1, "edit-spec.json")
SOURCE_KEY = work_key(AWEME, 0, "source.mp4")
AUDIO_KEY = work_key(AWEME, 1, "dubbed.wav")

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args], check=True)


def make_media(root: Path, *, source_s: float, audio_s: float) -> tuple[Path, Path]:
    """A plain grey portrait video with a 1 kHz sound of its own, and a 300 Hz voice."""
    source, audio = root / "source.mp4", root / "dubbed.wav"
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c=0x{SOURCE_GREY:02x}{SOURCE_GREY:02x}{SOURCE_GREY:02x}"
        f":s={W}x{H}:r={FPS}:d={source_s}",
        "-f", "lavfi", "-i", f"sine=frequency={SOURCE_HZ}:duration={source_s}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(source),
    )  # fmt: skip
    _ffmpeg(
        "-f", "lavfi", "-i", f"sine=frequency={VOICE_HZ}:duration={audio_s}",
        "-ar", "44100", "-ac", "2", str(audio),
    )  # fmt: skip
    return source, audio


def make_spec(audio: Path, **over: object) -> EditSpec:
    spec = {
        "source_key": SOURCE_KEY,
        "audio_key": AUDIO_KEY,
        "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "script_version": 2,
        "width": W,
        "height": H,
        "fps": FPS,
        "duration_ms": 3000,
        "cover": {"region": COVER, "color": COVER_HEX, "opacity_pct": COVER_PCT},
        "caption_box": COVER,
        "captions": CAPTIONS,
        "font": Path(FONT).name,
        "font_sha256": FONT_SHA,
    }
    spec.update(over)
    return EditSpec.model_validate(spec)


def new_store() -> ObjectStore:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="reup-media")
    return ObjectStore(client, "reup-media")


def put_inputs(store: ObjectStore, source: Path, audio: Path, spec: EditSpec) -> None:
    store.put_file(SOURCE_KEY, source)
    store.put_file(AUDIO_KEY, audio)
    # As the layout stage writes it.
    store.put_bytes(SPEC_KEY, spec.model_dump_json().encode(), content_type="application/json")


def run(activity: RenderActivity, spec_key: str = SPEC_KEY, env: ActivityEnvironment | None = None):
    env = env or ActivityEnvironment()
    return asyncio.run(env.run(activity.render, RenderInput(aweme_id=AWEME, spec_key=spec_key)))


def frame(video: Path, index: int) -> Image.Image:
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vf", f"select=eq(n\\,{index})",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    assert len(raw) == W * H * 3, f"frame {index} was not decoded"
    return Image.frombytes("RGB", (W, H), raw)


def white_rows(image: Image.Image, box: dict) -> list[tuple[int, int, int]]:
    """(row, first column, last column) of each row of the box holding text pixels."""
    crop = image.crop((box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"])).convert("L")
    width = crop.width
    data = crop.tobytes()
    rows = []
    for y in range(crop.height):
        columns = [x for x in range(width) if data[y * width + x] > 225]
        if columns:
            rows.append((y, columns[0], columns[-1]))
    return rows


def ink_height(rows: list[tuple[int, int, int]]) -> int:
    """Rows from the top of the highest accent to the bottom of the lowest descender. Two
    lines touch (one's descenders meet the next one's stacked marks), so they are told
    from one by their height: a 48 px line is under 60 rows, two 40 px lines are over 75."""
    return rows[-1][0] - rows[0][0] + 1 if rows else 0


def probe(video: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of",
         "json", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout  # fmt: skip
    return json.loads(out)


def close(pixel: tuple[int, ...], expected: tuple[float, ...], tolerance: float) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, expected, strict=True))


# --- one real render, looked at from every side --------------------------------------------


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("render")
    # The source is shorter than the spec: what is left is drawn over black, as the
    # composition's background was.
    source, audio = make_media(root, source_s=2.5, audio_s=2.6)
    spec = make_spec(audio)
    tmp_root = root / "scratch"
    tmp_root.mkdir()
    with mock_aws():
        store = new_store()
        put_inputs(store, source, audio, spec)
        activity = RenderActivity(store, font_path=FONT, tmp_root=tmp_root)
        result = run(activity)
        out = root / "out.mp4"
        store.download_file(result.render_key, out, produced_by="render")
        yield {
            "spec": spec,
            "result": result,
            "video": out,
            "store": store,
            "activity": activity,
            "tmp_root": tmp_root,
        }


def test_the_render_is_keyed_by_the_hash_the_layout_stage_gave_the_spec(rendered) -> None:
    spec, result = rendered["spec"], rendered["result"]
    assert isinstance(result, RenderResult)
    assert result.spec_sha256 == spec_sha256(spec)
    assert result.render_key == render_key(AWEME, spec_sha256(spec))
    assert result.render_key == f"work/{AWEME}/render-{spec_sha256(spec)}.mp4"


def test_the_video_has_the_spec_size_rate_and_length(rendered) -> None:
    body = probe(rendered["video"])
    (video,) = [s for s in body["streams"] if s["codec_type"] == "video"]
    (audio,) = [s for s in body["streams"] if s["codec_type"] == "audio"]
    assert (video["width"], video["height"]) == (W, H)
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
    assert video["r_frame_rate"] == f"{FPS}/1"
    assert int(video["nb_read_frames"]) == frame_count(3000, FPS) == 90
    assert audio["codec_name"] == "aac"
    # The voice is shorter than the video; the track still lasts as long as the picture.
    assert abs(float(audio["duration"]) - 3.0) <= 0.05
    assert abs(float(body["format"]["duration"]) - 3.0) <= 0.05


def test_the_moov_atom_comes_first_so_the_file_streams(rendered) -> None:
    head = rendered["video"].read_bytes()[:4096]
    assert head.find(b"moov") != -1 and head.find(b"moov") < max(head.find(b"mdat"), 4096)


def test_the_sound_is_the_dubbed_track_and_not_the_source(rendered) -> None:
    samples = decode(rendered["video"])
    # lavfi's sine is at 1/8 of full scale.
    assert level(samples, 200, 2400, VOICE_HZ) > 0.05
    assert level(samples, 200, 2400, SOURCE_HZ) < 0.005


def test_each_caption_is_shown_exactly_on_its_frames(rendered) -> None:
    video = rendered["video"]
    first_to = frame_at(1000, FPS)
    second_from, second_to = frame_at(1000, FPS), frame_at(2200, FPS)
    assert (first_to, second_from, second_to) == (30, 30, 66)
    # The first caption, one line, on frames 0..29.
    for index in (0, 15, first_to - 1):
        assert 20 < ink_height(white_rows(frame(video, index), COVER)) < 60, index
    # The second, two lines, from frame 30 up to and not including frame 66.
    for index in (second_from, 50, second_to - 1):
        assert ink_height(white_rows(frame(video, index), COVER)) > 75, index
    # Nothing after it.
    for index in (second_to, 80):
        assert white_rows(frame(video, index), COVER) == [], index


def test_a_caption_is_centred_in_its_box(rendered) -> None:
    rows = white_rows(frame(rendered["video"], 15), COVER)
    left = min(r[1] for r in rows)
    right = max(r[2] for r in rows)
    top, bottom = rows[0][0], rows[-1][0]
    assert abs((left + right) / 2 - COVER["w"] / 2) <= 3
    assert abs((top + bottom) / 2 - COVER["h"] / 2) <= 12
    # Drawn at its size: a 48 px line of this text is far wider than a 32 px one would be.
    assert right - left > 300


def test_the_two_lines_are_drawn_as_laid_out_and_never_rewrapped(rendered) -> None:
    rows = white_rows(frame(rendered["video"], 50), COVER)
    first = [r for r in rows if r[0] < rows[0][0] + 40]
    second = [r for r in rows if r[0] > rows[-1][0] - 40]
    # Each line is centred on its own, so the two have the same middle.
    mid = lambda rs: (min(r[1] for r in rs) + max(r[2] for r in rs)) / 2  # noqa: E731
    assert abs(mid(first) - mid(second)) <= 3
    # line-height 1.4 (28 px at 20 px) at 40 px: the second line is 56 px below the first.
    assert 40 <= rows[-1][0] - rows[0][0] <= 110


def test_the_cover_is_painted_in_its_colour_at_its_opacity(rendered) -> None:
    image = frame(rendered["video"], 45)
    alpha = COVER_PCT / 100
    expected = tuple(alpha * c + (1 - alpha) * SOURCE_GREY for c in COVER_RGB)
    corner = image.getpixel((COVER["x"] + 6, COVER["y"] + 6))
    assert close(corner, expected, 10), (corner, expected)
    outside = image.getpixel((COVER["x"] + 6, COVER["y"] - 20))
    assert close(outside, (SOURCE_GREY,) * 3, 6), outside


@pytest.mark.parametrize("with_captions", [True, False])
def test_timed_cover_leaves_caption_gaps_uncovered(tmp_path, with_captions) -> None:
    source, audio = make_media(tmp_path, source_s=3, audio_s=3)
    captions = [
        {**CAPTIONS[0], "start_ms": 500, "end_ms": 1000},
        {**CAPTIONS[1], "start_ms": 1500, "end_ms": 2200},
    ] if with_captions else []
    spec = make_spec(
        audio,
        cover={"region": COVER, "color": COVER_HEX, "opacity_pct": COVER_PCT,
               "only_with_captions": True},
        captions=captions,
    )
    out = tmp_path / "timed-cover.mp4"
    render_file(spec, source=source, audio=audio, font_path=FONT, out=out, work_dir=tmp_path)
    point = (COVER["x"] + 6, COVER["y"] + 6)
    for index in (0, 14, 30, 44, 66, 80):
        pixel = frame(out, index).getpixel(point)
        assert close(pixel, (SOURCE_GREY,) * 3, 6), (index, pixel)
    if with_captions:
        alpha = COVER_PCT / 100
        expected = tuple(alpha * c + (1 - alpha) * SOURCE_GREY for c in COVER_RGB)
        for index in (15, 29, 45, 65):
            assert close(frame(out, index).getpixel(point), expected, 10), index


def test_old_cover_specs_keep_their_hash_and_timed_covers_have_a_new_one(rendered) -> None:
    spec = rendered["spec"]
    original = spec.model_dump(mode="json")
    original["cover"].pop("only_with_captions", None)
    expected = json.dumps(original, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert canonical_spec_json(spec) == expected
    timed = spec.model_copy(update={
        "cover": spec.cover.model_copy(update={"only_with_captions": True}),
    })
    assert spec_sha256(timed) != spec_sha256(spec)


def test_the_text_casts_a_soft_shadow(rendered) -> None:
    image = frame(rendered["video"], 15).convert("L")
    rows = white_rows(frame(rendered["video"], 15), COVER)
    left = COVER["x"] + min(r[1] for r in rows)
    right = COVER["x"] + max(r[2] for r in rows)
    below = COVER["y"] + rows[-1][0] + 3
    cover_luma = image.getpixel((COVER["x"] + 6, COVER["y"] + 6))
    darkest = min(image.getpixel((x, below)) for x in range(left, right))
    assert darkest < cover_luma - 15


def test_after_the_source_ends_the_picture_is_black(rendered) -> None:
    image = frame(rendered["video"], 85)
    assert close(image.getpixel((W // 2, 200)), (0, 0, 0), 8)
    # The cover is still painted over it.
    alpha = COVER_PCT / 100
    covered = image.getpixel((COVER["x"] + 6, COVER["y"] + 6))
    assert close(covered, tuple(alpha * c for c in COVER_RGB), 10), covered


def test_a_spec_already_rendered_is_not_rendered_again(rendered) -> None:
    store: ObjectStore = rendered["store"]
    result: RenderResult = rendered["result"]
    before = store.get_bytes(result.render_key, produced_by="render")
    # Without its source the spec could not be rendered: the second call must not try.
    store._client.delete_object(Bucket="reup-media", Key=SOURCE_KEY)  # noqa: SLF001
    try:
        again = run(rendered["activity"])
    finally:
        store.put_bytes(SOURCE_KEY, b"")
    assert again == result
    assert store.get_bytes(result.render_key, produced_by="render") == before


def test_no_working_files_outlive_a_render(rendered) -> None:
    assert list(rendered["tmp_root"].iterdir()) == []


# --- what is refused, for good ---------------------------------------------------------------


@pytest.fixture
def inputs(tmp_path: Path):
    source, audio = make_media(tmp_path, source_s=0.5, audio_s=0.5)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with mock_aws():
        store = new_store()
        spec = make_spec(audio, duration_ms=500, captions=[])
        put_inputs(store, source, audio, spec)
        yield {"store": store, "spec": spec, "audio": audio, "scratch": scratch, "root": tmp_path}


def refused(activity: RenderActivity, spec_key: str = SPEC_KEY) -> ApplicationError:
    with pytest.raises(ApplicationError) as caught:
        run(activity, spec_key)
    return caught.value


def test_a_spec_outside_the_videos_work_folder_is_refused(inputs) -> None:
    activity = RenderActivity(inputs["store"], font_path=FONT, tmp_root=inputs["scratch"])
    # (A key that climbs out with ".." is already refused by the input's own contract.)
    error = refused(activity, "work/7002/r1/edit-spec.json")
    assert (error.type, error.non_retryable) == ("InvalidInput", True)


def test_a_spec_that_does_not_parse_or_validate_is_refused(inputs) -> None:
    store = inputs["store"]
    activity = RenderActivity(store, font_path=FONT, tmp_root=inputs["scratch"])
    store.put_bytes(SPEC_KEY, b"{not json")
    error = refused(activity)
    assert (error.type, error.non_retryable) == ("InvalidEditSpec", True)
    body = json.loads(inputs["spec"].model_dump_json())
    # A field out of range, and a frame over the area the render is sized for (a limit
    # the model checks in code, not in its schema).
    for over in ({"width": 0}, {"width": 1920, "height": 1920}):
        store.put_bytes(SPEC_KEY, json.dumps({**body, **over}).encode())
        assert refused(activity).type == "InvalidEditSpec", over


def test_a_spec_that_draws_another_videos_media_is_refused(inputs) -> None:
    store = inputs["store"]
    spec = inputs["spec"].model_copy(update={"source_key": "work/7002/r0/source.mp4"})
    store.put_bytes(SPEC_KEY, spec.model_dump_json().encode())
    error = refused(RenderActivity(store, font_path=FONT, tmp_root=inputs["scratch"]))
    assert (error.type, error.non_retryable) == ("InvalidEditSpec", True)


def test_a_font_other_than_the_one_the_layout_measured_with_is_refused(inputs) -> None:
    root: Path = inputs["root"]
    same_name = root / "other" / Path(FONT).name
    same_name.parent.mkdir()
    same_name.write_bytes(Path(FONT).read_bytes() + b"\0")
    renamed = root / "Renamed.ttf"
    renamed.write_bytes(Path(FONT).read_bytes())
    for font in (same_name, renamed):
        activity = RenderActivity(inputs["store"], font_path=str(font), tmp_root=inputs["scratch"])
        error = refused(activity)
        assert (error.type, error.non_retryable) == ("FontMismatch", True)


def test_audio_that_is_not_the_track_the_spec_was_made_for_is_refused(inputs) -> None:
    store = inputs["store"]
    store.put_bytes(AUDIO_KEY, inputs["audio"].read_bytes() + b"\0")
    error = refused(RenderActivity(store, font_path=FONT, tmp_root=inputs["scratch"]))
    assert (error.type, error.non_retryable) == ("AudioHashMismatch", True)
    assert list(inputs["scratch"].iterdir()) == []


def test_a_missing_spec_is_a_missing_artifact_of_the_layout(inputs) -> None:
    activity = RenderActivity(inputs["store"], font_path=FONT, tmp_root=inputs["scratch"])
    error = refused(activity, work_key(AWEME, 5, "edit-spec.json"))
    assert (error.type, error.non_retryable) == ("ArtifactMissingError", True)


# --- time: frames, deadline, heartbeat and cancellation -------------------------------------


def test_time_becomes_frames_as_the_composition_rounded_it() -> None:
    # Half a frame rounds up, as Math.round did; Python's round() would give 2.
    assert frame_at(100, 25) == 3
    assert frame_at(1000, 30) == 30
    assert frame_at(16, 30) == 0 and frame_at(17, 30) == 1
    assert frame_count(3000, 30) == 90
    assert frame_count(3010, 30) == 91  # a partial last frame is still drawn
    assert frame_count(1, 30) == 1


def test_the_deadline_grows_with_the_video() -> None:
    assert render_deadline_s(60_000) < render_deadline_s(600_000)
    # A render was measured at about 0.13 s per second of video; the allowance is far above.
    assert render_deadline_s(900_000) >= 5 * 60 + 900


def _ffmpeg_running(marker: Path) -> bool:
    found = subprocess.run(["pgrep", "-f", f"ffmpeg.*{marker}"], capture_output=True)
    return found.returncode == 0


@pytest.fixture
def long_render(tmp_path: Path):
    """A spec long enough that the render is still running when it is stopped."""
    source, audio = make_media(tmp_path, source_s=1.0, audio_s=1.0)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with mock_aws():
        store = new_store()
        put_inputs(store, source, audio, make_spec(audio, duration_ms=600_000))
        yield store, scratch


def test_a_cancelled_render_kills_ffmpeg_and_leaves_nothing_behind(long_render) -> None:
    store, scratch = long_render
    beats: list[object] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    activity = RenderActivity(store, font_path=FONT, tmp_root=scratch, heartbeat_every=0.1)

    async def scenario() -> None:
        task = asyncio.ensure_future(
            env.run(activity.render, RenderInput(aweme_id=AWEME, spec_key=SPEC_KEY))
        )
        for _ in range(300):
            if _ffmpeg_running(scratch):
                break
            await asyncio.sleep(0.05)
        assert _ffmpeg_running(scratch), "ffmpeg never started"
        await asyncio.sleep(0.3)
        env.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert beats, "the render did not heartbeat while it ran"
    assert not _ffmpeg_running(scratch)
    assert list(scratch.iterdir()) == []
    assert not store.exists(render_key(AWEME, spec_sha256(EditSpec.model_validate_json(
        store.get_bytes(SPEC_KEY, produced_by="captions")))))  # fmt: skip


def test_a_render_past_its_deadline_is_stopped_and_refused(long_render) -> None:
    store, scratch = long_render
    activity = RenderActivity(store, font_path=FONT, tmp_root=scratch, deadline_s=1.0)
    error = refused(activity)
    assert (error.type, error.non_retryable) == ("RenderDeadline", True)
    assert not _ffmpeg_running(scratch)
    assert list(scratch.iterdir()) == []


# --- the spec's hash -------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(samples()))
def test_the_hash_is_taken_over_the_bytes_the_layout_stage_stores(name: str) -> None:
    """The layout stage returns `spec_sha256(spec)` and stores `model_dump_json()`; the
    render reads those bytes back and must arrive at the same hash."""
    from localization.activities.render import read_spec

    spec = samples()[name]
    assert spec_sha256(read_spec(spec.model_dump_json().encode())) == spec_sha256(spec)
