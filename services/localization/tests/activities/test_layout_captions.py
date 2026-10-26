"""Step 4b: lines are broken and sized with the caption font, a caption is shown while
its line is spoken, the cover is the burned-in subtitle grown by 6%, and the stored spec
carries the script version and the audio hash it was made for."""

import base64

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.layout_captions import (
    BOX_PADDING,
    FONT_SIZES,
    FontMeter,
    LayoutCaptionsActivity,
    fit,
    grow_cover,
    safe_zone,
    split_to_fit,
)
from localization.activities.script_document import script_key
from localization.activities.synthesize import META_TEXT, Synthesizer
from localization.config import DEFAULT_CAPTION_FONT
from localization.contracts import (
    BBox,
    DubbedAudio,
    DubSegment,
    DubSegmentsDocument,
    EditSpec,
    IngestResult,
    LocalizedScriptRef,
    SubtitleTrack,
)
from localization.contracts.spec_hash import spec_sha256
from localization.storage.object_store import ObjectStore, work_key
from tests.support.inputs import make_input
from tests.support.tts_fakes import FakeTts, put_script, segment

SPEC = make_input()
FONT = str(DEFAULT_CAPTION_FONT)
AUDIO_SHA = "b" * 64
VIDEO_MS = 20_000
BOX_W = 1000
MAX_W = BOX_W * (1 - 2 * BOX_PADDING)

meter = FontMeter(FONT)
BURNED_IN = BBox(x=40, y=1500, w=1000, h=100)


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        client.put_object(Bucket="reup-media", Key="source/7001.mp4", Body=b"video-bytes")
        yield ObjectStore(client, "reup-media")


def ingest(*, width: int = 1080, height: int = 1920) -> IngestResult:
    return IngestResult(
        vocals_key=work_key("7001", 0, "vocals.wav"),
        bgm_key=work_key("7001", 0, "bgm.wav"),
        duration_ms=VIDEO_MS,
        width=width,
        height=height,
        fps=30,
    )


def lines_fit(lines, size: int, max_width: float = MAX_W) -> bool:
    return all(meter.width(line, size) <= max_width for line in lines)


# --- line breaking and size ------------------------------------------------------


def test_a_short_line_is_one_line_at_the_largest_size() -> None:
    fitted = fit("Xin chào các bạn", meter, MAX_W)
    assert fitted is not None
    assert fitted.lines == ("Xin chào các bạn",)
    assert fitted.size == FONT_SIZES[0]


def test_a_longer_line_takes_two_lines_and_the_largest_size_that_fits() -> None:
    text = "Hôm nay chúng ta sẽ cùng nhau học cách nấu một nồi phở bò thật ngon tại nhà"
    fitted = fit(text, meter, MAX_W)
    assert fitted is not None
    assert len(fitted.lines) == 2
    assert " ".join(fitted.lines) == text
    assert lines_fit(fitted.lines, fitted.size)
    # Nothing larger would have fitted: not on one line, and not on two.
    larger = [s for s in FONT_SIZES if s > fitted.size]
    assert all(meter.width(text, s) > MAX_W for s in larger)
    for size in larger:
        words = text.split()
        splits = ((" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words)))
        assert not any(lines_fit(pair, size) for pair in splits)


def test_the_two_lines_are_balanced() -> None:
    text = "Hôm nay chúng ta sẽ cùng nhau học cách nấu một nồi phở bò thật ngon tại nhà"
    fitted = fit(text, meter, MAX_W)
    assert fitted is not None
    first, second = (meter.width(line, fitted.size) for line in fitted.lines)
    assert abs(first - second) < 0.35 * max(first, second)


def test_a_line_that_fits_in_no_size_in_two_lines_is_not_fitted_at_all() -> None:
    assert fit("từ " * 60, meter, MAX_W) is None


def test_a_long_line_becomes_cues_that_each_fit_and_say_it_all_in_order() -> None:
    words = ("Chúng ta cùng đi qua cánh đồng lúa chín vàng dưới nắng chiều " * 3).split()
    pieces = split_to_fit(" ".join(words), meter, MAX_W)
    assert len(pieces) > 1
    for piece in pieces:
        assert len(piece.lines) <= 2
        assert lines_fit(piece.lines, piece.size)
    assert [w for p in pieces for line in p.lines for w in line.split()] == words


def test_a_single_word_wider_than_the_box_is_kept_whole_at_the_smallest_size() -> None:
    pieces = split_to_fit("a " + "x" * 200 + " b", meter, MAX_W)
    assert [p.lines for p in pieces if "x" * 200 in p.lines[0]] == [("x" * 200,)]
    assert all(p.size == FONT_SIZES[-1] for p in pieces if "x" * 200 in p.lines[0])
    assert [w for p in pieces for line in p.lines for w in line.split()] == [
        "a",
        "x" * 200,
        "b",
    ]


# --- the cover -------------------------------------------------------------------


def test_the_cover_is_the_burned_in_region_grown_by_six_percent_on_each_side() -> None:
    grown = grow_cover(BBox(x=100, y=1500, w=800, h=100), 1080, 1920)
    assert (grown.x, grown.y, grown.w, grown.h) == (52, 1494, 896, 112)


def test_the_cover_never_leaves_the_frame() -> None:
    grown = grow_cover(BBox(x=0, y=1850, w=1080, h=70), 1080, 1920)
    assert grown.x == 0 and grown.x + grown.w == 1080
    assert grown.y + grown.h == 1920


def test_no_burned_in_subtitle_means_no_cover_and_captions_in_the_safe_zone() -> None:
    zone = safe_zone(1080, 1920)
    assert zone.x >= 0 and zone.x + zone.w <= 1080
    assert zone.y >= 1920 * 0.5 and zone.y + zone.h <= 1920


# --- the activity ----------------------------------------------------------------


async def stage(
    store: ObjectStore,
    texts: list[str],
    *,
    slot_ms: int = 2000,
    region: BBox | None = BURNED_IN,
    script_version: int = 2,
    spoken: dict[int, str] | None = None,
    seconds_per_char: float = 0.05,
):
    """A script, real clips (through the synthesizer, so they carry the metadata the
    renderer's audio was made from), and a dub segments document over them."""
    segments = [segment(i, t, slot_ms=slot_ms, start_ms=i * 3000) for i, t in enumerate(texts)]
    key = put_script(store, segments, version=script_version)
    tts = FakeTts(seconds_per_char=seconds_per_char)
    synth = Synthesizer(store, tts, model="tts-1")
    doc_segments = []
    for seg in segments:
        said = (spoken or {}).get(seg.id, seg.text_vi)
        clip = await synth.clip(SPEC.voice, "eng", said)
        doc_segments.append(
            DubSegment(
                id=seg.id,
                text_vi=said,
                tts_key=clip.key,
                duration_ms=clip.duration_ms,
                stretch_ratio=1.0,
                overflow_unresolved=False,
            )
        )
    segments_key = work_key("7001", 1, "dub-segments.json")
    document = DubSegmentsDocument(
        aweme_id="7001", script_version=script_version, segments=tuple(doc_segments)
    )
    store.put_bytes(segments_key, document.model_dump_json().encode())
    ref = LocalizedScriptRef(script_key=key, version=script_version)
    dub = DubbedAudio(
        audio_key=work_key("7001", 1, "dubbed.wav"),
        audio_sha256=AUDIO_SHA,
        script_key=key,
        script_version=script_version,
        segments_key=segments_key,
    )
    return ref, SubtitleTrack(subtitle_key=work_key("7001", 0, "subs.json"), region=region), dub


def layout(store: ObjectStore) -> LayoutCaptionsActivity:
    return LayoutCaptionsActivity(store, font_path=FONT, cover_color="#101010", cover_opacity=0.92)


async def run(store, ref, subs, dub, *, prep=None, round: int = 1):
    ref_out = await ActivityEnvironment().run(
        layout(store).layout_captions, SPEC, ref, subs, prep or ingest(), dub, round
    )
    stored = EditSpec.model_validate_json(store.get_bytes(ref_out.spec_key, produced_by="test"))
    return ref_out, stored


async def test_the_spec_is_stored_under_the_round_and_carries_what_it_was_made_for(
    store: ObjectStore,
) -> None:
    ref, subs, dub = await stage(store, ["Xin chào", "Tạm biệt"])
    out, spec = await run(store, ref, subs, dub, round=3)
    assert out.spec_key == work_key("7001", 3, "edit-spec.json")
    assert spec.script_version == ref.version == out.script_version
    assert spec.audio_sha256 == AUDIO_SHA == out.audio_sha256
    assert spec.audio_key == dub.audio_key
    assert (spec.width, spec.height, spec.fps, spec.duration_ms) == (1080, 1920, 30, VIDEO_MS)
    assert spec.font == "BeVietnamPro-SemiBold.ttf"


async def test_the_returned_hash_is_the_hash_of_the_stored_spec(store: ObjectStore) -> None:
    ref, subs, dub = await stage(store, ["Xin chào"])
    out, spec = await run(store, ref, subs, dub)
    assert out.spec_sha256 == spec_sha256(spec)


async def test_the_cover_is_grown_and_the_captions_sit_in_it(store: ObjectStore) -> None:
    ref, subs, dub = await stage(store, ["Xin chào"])
    _, spec = await run(store, ref, subs, dub)
    assert spec.cover is not None
    assert spec.cover.region == grow_cover(subs.region, 1080, 1920)
    assert (spec.cover.color, spec.cover.opacity_pct) == ("#101010", 92)
    assert spec.caption_box == spec.cover.region


async def test_without_a_burned_in_subtitle_there_is_no_cover(store: ObjectStore) -> None:
    ref, subs, dub = await stage(store, ["Xin chào"], region=None)
    _, spec = await run(store, ref, subs, dub)
    assert spec.cover is None
    assert spec.caption_box == safe_zone(1080, 1920)


async def test_every_line_of_every_caption_fits_the_box_at_its_own_size(
    store: ObjectStore,
) -> None:
    texts = [
        "Xin chào",
        "Hôm nay chúng ta sẽ cùng nhau học cách nấu một nồi phở bò thật ngon tại nhà",
        "Chúng ta cùng đi qua cánh đồng lúa chín vàng dưới nắng chiều " * 3,
    ]
    ref, subs, dub = await stage(store, texts)
    _, spec = await run(store, ref, subs, dub)
    max_width = spec.caption_box.w * (1 - 2 * BOX_PADDING)
    assert len(spec.captions) > len(texts)  # the long one was split
    for caption in spec.captions:
        lines = caption.text.split("\n")
        assert len(lines) <= 2
        assert lines_fit(lines, caption.font_px, max_width)


async def test_captions_say_what_the_voice_was_made_from_even_when_a_cue_was_split(
    store: ObjectStore,
) -> None:
    texts = ["Xin chào", "Chúng ta cùng đi qua cánh đồng lúa chín vàng dưới nắng chiều " * 3]
    ref, subs, dub = await stage(store, texts)
    _, spec = await run(store, ref, subs, dub)
    doc = DubSegmentsDocument.model_validate_json(
        store.get_bytes(dub.segments_key, produced_by="test")
    )
    for seg in doc.segments:
        shown = " ".join(c.text.replace("\n", " ") for c in spec.captions if c.segment_id == seg.id)
        meta = store.metadata(seg.tts_key)
        assert meta is not None
        assert shown == base64.b64decode(meta[META_TEXT]).decode().strip()


async def test_a_caption_is_shown_while_its_line_is_spoken_and_never_into_the_next(
    store: ObjectStore,
) -> None:
    # Segment 0 starts at 0 and its slot ends at 2000; the clip is 5 s long, so the
    # line is still being spoken when segment 1 starts at 3000.
    ref, subs, dub = await stage(store, ["x" * 100, "Tạm biệt"], seconds_per_char=0.05)
    _, spec = await run(store, ref, subs, dub)
    first = [c for c in spec.captions if c.segment_id == 0]
    second = [c for c in spec.captions if c.segment_id == 1]
    assert (first[0].start_ms, first[-1].end_ms) == (0, 3000)
    assert second[0].start_ms == 3000


async def test_pieces_of_a_split_cue_tile_its_interval_without_gap_or_overlap(
    store: ObjectStore,
) -> None:
    ref, subs, dub = await stage(
        store, ["Chúng ta cùng đi qua cánh đồng lúa chín vàng dưới nắng chiều " * 3]
    )
    _, spec = await run(store, ref, subs, dub)
    cues = list(spec.captions)
    assert len(cues) > 1
    assert cues[0].start_ms == 0
    for before, after in zip(cues, cues[1:], strict=False):
        assert before.end_ms == after.start_ms


async def test_a_caption_never_outlasts_the_video(store: ObjectStore) -> None:
    ref, subs, dub = await stage(
        store,
        ["x" * 20] * 7,
        slot_ms=5000,
        seconds_per_char=0.5,  # segment 6 starts at 18 s
    )
    _, spec = await run(store, ref, subs, dub)
    assert max(c.end_ms for c in spec.captions) == VIDEO_MS


async def test_a_segment_that_starts_after_the_video_ends_has_no_caption(
    store: ObjectStore,
) -> None:
    ref, subs, dub = await stage(store, ["một"] * 8)  # segment 7 starts at 21 s
    _, spec = await run(store, ref, subs, dub)
    assert {c.segment_id for c in spec.captions} == set(range(7))


async def test_the_source_video_is_copied_into_work_for_the_renderer(
    store: ObjectStore,
) -> None:
    ref, subs, dub = await stage(store, ["Xin chào"])
    _, spec = await run(store, ref, subs, dub)
    assert spec.source_key == work_key("7001", 0, "source.mp4")
    assert store.get_bytes(spec.source_key, produced_by="test") == b"video-bytes"


async def test_laying_out_twice_gives_the_same_spec_and_the_same_hash(
    store: ObjectStore,
) -> None:
    ref, subs, dub = await stage(store, ["Xin chào", "Tạm biệt"])
    first, _ = await run(store, ref, subs, dub)
    second, _ = await run(store, ref, subs, dub)
    assert first == second


async def test_a_track_made_from_another_script_version_is_refused_for_good(
    store: ObjectStore,
) -> None:
    ref, subs, dub = await stage(store, ["Xin chào"], script_version=2)
    newer = LocalizedScriptRef(script_key=script_key("7001", 3), version=3)
    put_script(store, [segment(0, "Xin chào")], version=3)  # same words, newer version
    with pytest.raises(ApplicationError) as info:
        await run(store, newer, subs, dub)
    assert info.value.type == "ScriptDubMismatch"
    assert info.value.non_retryable is True


async def test_a_line_spoken_differently_from_how_it_is_written_is_refused_for_good(
    store: ObjectStore,
) -> None:
    ref, subs, dub = await stage(store, ["Xin chào", "Tạm biệt"], spoken={1: "Hẹn gặp lại"})
    with pytest.raises(ApplicationError) as info:
        await run(store, ref, subs, dub)
    assert info.value.type == "ScriptDubMismatch"
    assert info.value.non_retryable is True


async def test_nothing_is_stored_when_the_layout_is_refused(store: ObjectStore) -> None:
    ref, subs, dub = await stage(store, ["Xin chào"], spoken={0: "khác"})
    with pytest.raises(ApplicationError):
        await run(store, ref, subs, dub)
    assert not store.exists(work_key("7001", 1, "edit-spec.json"))
