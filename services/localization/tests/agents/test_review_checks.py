"""The review's checks that need no model. Each has a case that fails and one that passes,
and the issues come back in the shape the workflow routes on."""

from fractions import Fraction

import pytest

from localization.activities.layout_captions import FontMeter
from localization.agents import review_checks as checks
from localization.agents.reviewer import Loudness
from localization.config import DEFAULT_CAPTION_FONT
from localization.contracts import (
    BBox,
    Caption,
    Cover,
    DubSegment,
    DubSegmentsDocument,
    EditSpec,
    LocalizedSegment,
    SubtitleCue,
    SubtitleDocument,
)
from localization.contracts.models import STRETCH_MAX

TARGET, PEAK = -14.0, -1.5
GOOD_AUDIO = Loudness(lufs=-14.2, true_peak_dbtp=-1.6)
BOX = BBox(x=20, y=380, w=230, h=60)


def seg(i: int, zh: str = "你好", vi: str = "xin chào", start: int = 0, slot: int = 2000):
    return LocalizedSegment(
        id=i,
        start_ms=start,
        end_ms=start + 500,
        slot_ms=slot,
        src_zh=zh,
        text_vi=vi,
        on_screen=False,
    )


def dub(*rows: tuple[int, int, float, bool]) -> DubSegmentsDocument:
    return DubSegmentsDocument(
        aweme_id="7001",
        script_version=1,
        segments=tuple(
            DubSegment(
                id=i,
                text_vi="x",
                tts_key=f"cache/tts/{i:064x}.wav",
                duration_ms=ms,
                stretch_ratio=ratio,
                overflow_unresolved=over,
            )
            for i, ms, ratio, over in rows
        ),
    )


def timing(script, doc, *, duration=10_000, audio=GOOD_AUDIO):  # noqa: ANN001, ANN201
    return checks.timing_findings(
        script, doc, duration, audio, target_lufs=TARGET, true_peak_dbtp=PEAK
    )


def only(found: list[checks.Finding]) -> checks.Finding:
    assert len(found) == 1, found
    return found[0]


# --- the lines ----------------------------------------------------------------------------


def test_a_line_with_nothing_in_it_is_a_translation_problem_of_that_line() -> None:
    f = only(checks.line_findings([seg(3, vi="  ")]))
    assert (f.dimension, f.issue.stage, f.issue.segment_id) == ("fidelity", "localize", 3)
    assert "empty" in f.issue.reason


def test_a_line_that_still_has_chinese_names_the_characters() -> None:
    f = only(checks.line_findings([seg(1, zh="明镜", vi="Ủng hộ 明镜")]))
    assert f.issue.stage == "localize" and "明" in f.issue.reason
    assert checks.line_findings([seg(1, zh="明镜", vi="Ủng hộ Minh Kính")]) == []


def test_a_number_of_the_source_must_reach_the_line_as_digits_or_as_words() -> None:
    lost = only(checks.line_findings([seg(0, zh="只要30元", vi="Chỉ rất rẻ")]))
    assert "30" in lost.issue.reason
    assert checks.line_findings([seg(0, zh="只要30元", vi="Chỉ 30 nghìn")]) == []
    assert checks.line_findings([seg(0, zh="只要30元", vi="Chỉ ba mươi nghìn")]) == []
    assert checks.line_findings([seg(0, zh="共1500元", vi="Tổng cộng 1.500 đồng")]) == []


@pytest.mark.parametrize(
    ("zh", "vi"),
    [
        ("30岁更像20岁", "Ba mươi tuổi mà trông như hai mươi"),
        ("30岁更像20岁", "Ba mươi tuổi mà trông như đôi mươi"),
        ("4.6安纳铁", "bốn phẩy sáu miligam"),
        ("4.6安纳铁", "4,6 mg"),
        ("每100个女生70个", "cứ một trăm bạn nữ thì bảy mươi bạn"),
    ],
)
def test_a_number_spelled_in_vietnamese_words_is_carried(zh: str, vi: str) -> None:
    assert checks.line_findings([seg(0, zh=zh, vi=vi)]) == []


@pytest.mark.parametrize(
    ("zh", "vi", "missing"),
    [
        ("每100个女生70个", "cứ một trăm bạn nữ thì có bạn", "the source has 70,"),
        ("每100个女生70个", "cứ một trăm bạn nữ thì bảy mươi lăm bạn", "the source has 70,"),
        ("4.6安纳铁", "sắt", "the source has 4.6,"),
    ],
)
def test_a_number_dropped_or_changed_is_named(zh: str, vi: str, missing: str) -> None:
    assert missing in only(checks.line_findings([seg(0, zh=zh, vi=vi)])).issue.reason


def test_a_latin_name_of_the_source_must_reach_the_line() -> None:
    lost = only(checks.line_findings([seg(0, zh="用iPhone拍的", vi="Quay bằng điện thoại")]))
    assert "iPhone" in lost.issue.reason
    assert checks.line_findings([seg(0, zh="用iPhone拍的", vi="Quay bằng iphone")]) == []


# --- timing -------------------------------------------------------------------------------


def test_a_line_that_still_overruns_its_slot_goes_back_to_the_dubber_for_that_line() -> None:
    f = only(timing([seg(2, slot=800)], dub((2, 1500, 1.15, True))))
    assert (f.dimension, f.issue.stage, f.issue.segment_id) == ("timing", "dub", 2)
    assert "overruns" in f.issue.reason and "800 ms slot" in f.issue.reason
    assert timing([seg(2, slot=800)], dub((2, 700, 1.0, False))) == []


def test_a_line_stretched_to_the_edge_is_read_too_fast_or_too_slow() -> None:
    fast = only(timing([seg(0)], dub((0, 1900, STRETCH_MAX, False))))
    assert "too fast" in fast.issue.reason and fast.issue.stage == "dub"
    slow = only(timing([seg(0)], dub((0, 1000, 0.87, False))))
    assert "too slow" in slow.issue.reason
    assert (
        timing(
            [seg(0)],
            dub(
                (0, 1900, 1.10, False),
            ),
        )
        == []
    )
    assert timing([seg(0)], dub((0, 1900, 0.90, False))) == []


def test_a_line_the_dubber_was_allowed_to_speed_up_is_not_read_too_fast() -> None:
    # The review's limit is the stretch the dubbing stage holds lines to, not a copy of it.
    assert checks.FAST_RATIO is STRETCH_MAX
    assert timing([seg(0)], dub((0, 1900, 1.13, False))) == []
    assert timing([seg(0)], dub((0, 1900, 1.149, False))) == []


def test_a_line_that_the_end_of_the_video_cuts_off_is_named() -> None:
    script = [seg(0, start=9_500)]
    f = only(timing(script, dub((0, 1200, 1.0, False)), duration=10_000))
    assert f.issue.segment_id == 0 and "cut off" in f.issue.reason and "500 ms" in f.issue.reason
    assert timing(script, dub((0, 400, 1.0, False)), duration=10_000) == []


def test_an_overrun_is_reported_once_not_also_as_fast() -> None:
    assert len(timing([seg(0)], dub((0, 3000, 1.15, True)))) == 1


@pytest.mark.parametrize(
    ("audio", "words"),
    [
        (Loudness(-20.0, -3.0), "LUFS"),
        (Loudness(-9.0, -1.0), "LUFS"),
        (Loudness(-14.0, 0.5), "dBTP"),
        (Loudness(float("-inf"), -90.0), "silent"),
    ],
)
def test_audio_off_its_loudness_limits_is_a_problem_for_the_render_stage(
    audio: Loudness, words: str
) -> None:
    f = only(timing([seg(0)], dub((0, 500, 1.0, False)), audio=audio))
    assert (f.issue.stage, f.issue.segment_id) == ("render", None) and words in f.issue.reason


def test_audio_within_the_limits_passes() -> None:
    edge = Loudness(TARGET + checks.LUFS_TOLERANCE, PEAK + checks.PEAK_TOLERANCE)
    assert timing([seg(0)], dub((0, 500, 1.0, False)), audio=edge) == []


# --- the picture --------------------------------------------------------------------------


def spec_with(*captions: Caption, cover: bool = True) -> EditSpec:
    return EditSpec(
        source_key="work/7001/r0/source.mp4",
        audio_key="work/7001/r1/dubbed.wav",
        audio_sha256="a" * 64,
        script_version=1,
        width=270,
        height=480,
        fps=10,
        duration_ms=8_000,
        cover=Cover(region=BOX, color="#101010", opacity_pct=92) if cover else None,
        caption_box=BOX,
        captions=captions,
        font="f.ttf",
        font_sha256="b" * 64,
    )


def caption(i: int, text: str, px: int = 32, start: int = 0) -> Caption:
    return Caption(segment_id=i, start_ms=start, end_ms=start + 1000, text=text, font_px=px)


@pytest.fixture(scope="module")
def meter() -> FontMeter:
    return FontMeter(str(DEFAULT_CAPTION_FONT))


def test_a_caption_line_wider_than_its_box_is_a_layout_problem(meter: FontMeter) -> None:
    wide = "Siêuphẩmkhôngthểnàođượcchiasẻ"
    f = only(checks.caption_findings(spec_with(caption(4, wide, px=48)), meter))
    assert (f.dimension, f.issue.stage, f.issue.segment_id) == ("visual", "captions", 4)
    assert "230 px box" in f.issue.reason


def test_captions_that_fit_pass(meter: FontMeter) -> None:
    spec = spec_with(caption(0, "Xin chào bạn", px=32), caption(1, "Chia sẻ\nnhé", px=48))
    assert checks.caption_findings(spec, meter) == []


# Left-over subtitle is judged on what the subtitle detector saw on the source, frame by
# frame, against the cover painted over it. The shapes are those of a real portrait video:
# a 1080x1920 frame, a cover grown to the two-line caption box, a box every 500 ms.
PORTRAIT_COVER = BBox(x=48, y=1455, w=969, h=125)
INTERVAL = 500


def portrait_spec(cover: BBox | None = PORTRAIT_COVER) -> EditSpec:
    return EditSpec(
        source_key="work/7001/r0/source.mp4",
        audio_key="work/7001/r1/dubbed.wav",
        audio_sha256="a" * 64,
        script_version=1,
        width=1080,
        height=1920,
        fps=30,
        duration_ms=60_000,
        cover=None if cover is None else Cover(region=cover, color="#101010", opacity_pct=92),
        caption_box=cover or BBox(x=54, y=1498, w=972, h=269),
        captions=(),
        font="f.ttf",
        font_sha256="b" * 64,
    )


def line_at(i: int) -> BBox:
    """The source subtitle where the cover is: lines of different lengths, the model's
    box a few pixels off from frame to frame."""
    return BBox(x=300 - (i % 5) * 40, y=1490 + (i % 4) * 8, w=480 + (i % 5) * 80, h=70)


BELOW = BBox(x=210, y=1620, w=660, h=80)  # a line under the cover's bottom edge (1580)


def subtitles(
    boxes: list[BBox | None],
    unusable: tuple[int, ...] = (),
    *,
    size: tuple[int, int] = (1080, 1920),
) -> SubtitleDocument:
    return SubtitleDocument(
        width=size[0],
        height=size[1],
        region=BBox(x=53, y=1460, w=959, h=115),
        cues=(SubtitleCue(start_ms=0, end_ms=len(boxes) * INTERVAL),),
        frame_interval_ms=INTERVAL,
        frame_boxes=tuple(None if i in unusable else b for i, b in enumerate(boxes)),
        unusable_frames=unusable,
    )


def frames(inside: int, below: int) -> list[BBox | None]:
    """`inside` frames of the line under the cover, then `below` frames of a line the
    cover misses, after a start with no subtitle on screen."""
    return [None] * 4 + [line_at(i) for i in range(inside)] + [BELOW] * below


def test_a_subtitle_under_the_cover_with_one_stray_box_and_unreadable_frames_is_hidden() -> None:
    boxes = frames(100, 0)
    boxes[50] = BBox(x=219, y=292, w=636, h=88)  # a title near the top, seen once
    boxes[70] = BBox(x=150, y=1640, w=700, h=90)  # one frame the model placed too low
    assert checks.residual_findings(portrait_spec(), subtitles(boxes, unusable=(9, 10, 33))) == []


def test_a_box_looser_than_the_caption_box_is_the_subtitle_under_it() -> None:
    # The cover and the box the model drew around the line under it on a real render:
    # it starts on the cover's top edge and runs 39 px under its bottom one.
    boxes: list[BBox | None] = [BBox(x=54, y=1455, w=972, h=164)] * 160
    assert checks.residual_findings(portrait_spec(), subtitles(boxes)) == []


def test_a_subtitle_below_the_cover_in_many_frames_is_left_over_subtitle() -> None:
    f = only(checks.residual_findings(portrait_spec(), subtitles(frames(60, 40))))
    assert (f.dimension, f.issue.stage, f.issue.segment_id) == ("visual", "render", None)
    assert f.issue.reason.startswith("source text is burned in next to the caption")
    assert f.issue.reason.endswith("left-over source subtitle")
    assert "(x=210 y=1620 w=660 h=80)" in f.issue.reason
    first = (4 + 60) * INTERVAL
    assert f"at {first}, {first + 500}, {first + 1000}, {first + 1500} ms" in f.issue.reason


def test_boxes_misplaced_on_a_fifth_of_the_frames_are_the_models_error() -> None:
    # The worst seen on a video whose cover hid every line: 22% of the frames with a box put
    # it mostly outside the cover, eight frames in a row of them, while the line was under it.
    boxes = frames(76, 0)
    for i in [*range(30, 38), *range(50, 76, 2)]:
        boxes[i] = BBox(x=190, y=1540 + (i % 5) * 30, w=700, h=110)
    assert checks.residual_findings(portrait_spec(), subtitles(boxes)) == []


# The boxes the detector stored for a real video (one every 500 ms, "-" for a frame
# without one) and the cover of its final edit spec. The line sat at y 1385-1465 on every
# frame checked (0.5, 2, 21 and 45 s), under the cover; on about a third of the frames the
# model put its box 70-90 px lower, in and out between frames that showed one line.
COVER_7616 = BBox(x=5, y=1359, w=1064, h=138)
BOXES_7616 = """
    429 1378 169 111, 418 1444 182 150, 444 1459 180 82, 439 1365 191 98, 358 1453 364 109,
    328 1449 424 105, 311 1451 461 107, 188 1551 442 77, 191 1388 698 107, 202 1382 392 77,
    202 1388 673 64, 420 1384 238 86, 418 1374 171 90, 418 1386 232 75, 411 1382 244 90,
    428 1359 216 90, 98 1372 885 90, 96 1200 849 105, 86 1372 476 92, 75 1459 936 81, -,
    41 1391 996 120, 32 1395 981 79, 124 1457 838 98, 112 1378 922 144, 131 1376 832 105,
    208 1449 664 116, 210 1378 662 112, 204 1374 651 107, 225 1382 630 109, -, -, -, -,
    172 998 116 107, 182 943 101 201, 43 979 118 366, 88 909 234 482, 414 1386 270 77,
    375 1378 296 86, 351 1455 377 103, 73 1462 934 103, 88 1466 900 82, 0 1524 621 139,
    56 1464 966 92, 49 1380 982 141, 81 1395 932 118, 49 1446 982 118, 56 1397 924 120,
    88 1434 938 103, 62 1487 951 88, 206 1658 656 105, 144 1605 789 118, 197 1689 686 81,
    176 1545 718 105, 208 1656 664 96, 202 1695 673 107, 189 1575 733 148, 216 1590 626 131,
    189 1378 694 111, 208 1391 668 135, 180 1506 739 156, 206 1311 641 112, 152 1487 804 172,
    163 1571 844 137, 352 1376 232 96, 384 1361 253 139, 356 1352 313 116, 156 1449 774 148,
    141 1498 767 172, 146 1376 788 118, 133 1358 816 137, 347 1266 446 189, 394 1341 287 129,
    159 1296 761 169, 304 1365 472 94, 248 1273 591 189, 54 1359 928 133, 43 1444 968 107,
    41 1371 1001 172, -, 41 1371 998 111, 22 1519 986 165, 159 1476 737 77, 186 1388 707 88,
    193 1391 654 69, 249 1386 587 84, 212 1470 587 88, 234 1391 613 73, 47 1455 984 107,
    58 1459 973 99, 26 1464 998 107, 49 1432 973 129, 47 1466 1014 116, 92 1372 489 109,
    84 1348 638 146, 86 1371 645 122, 81 1371 679 124, -, -, -, -, -, -,
    43 1489 982 135, 45 1605 994 98, 45 1541 990 122, 49 1635 996 112, 52 1543 973 135,
    30 1491 1020 146, 152 1369 778 128, 156 1524 446 94, 152 1521 463 107, 354 1406 341 92,
    379 1372 278 84, -, 197 1369 686 103, 188 1378 705 84, 189 1453 701 109, -,
    300 1372 486 94, 266 1358 596 79, 326 1380 399 79, 324 1485 426 107, 311 1446 480 111,
    315 1438 476 98, 328 1386 343 129, 308 1404 354 107, 120 1530 851 84, 66 1547 909 105,
    109 1461 866 96, 146 1462 857 86, 133 1472 821 156, -,
"""


def boxes_7616() -> list[BBox | None]:
    found: list[BBox | None] = []
    for item in (part.strip() for part in BOXES_7616.split(",")):
        if item == "-":
            found.append(None)
        elif item:
            x, y, w, h = (int(n) for n in item.split())
            found.append(BBox(x=x, y=y, w=w, h=h))
    return found


def moved(box: BBox | None, dy: int) -> BBox | None:
    return None if box is None else box.model_copy(update={"y": box.y + dy})


def test_a_line_the_model_puts_lower_now_and_then_is_not_left_over_subtitle() -> None:
    boxes = boxes_7616()
    assert len(boxes) == 134
    assert checks.residual_findings(portrait_spec(COVER_7616), subtitles(boxes)) == []


@pytest.mark.parametrize("up", [80, 150])
def test_a_cover_above_the_line_is_reported_on_the_real_boxes(up: int) -> None:
    cover = COVER_7616.model_copy(update={"y": COVER_7616.y - up})
    assert len(checks.residual_findings(portrait_spec(cover), subtitles(boxes_7616()))) == 1


def test_a_line_that_moves_below_the_cover_halfway_is_reported_on_the_real_boxes() -> None:
    boxes = boxes_7616()
    half = len(boxes) // 2
    boxes = boxes[:half] + [moved(b, 160) for b in boxes[half:]]
    assert len(checks.residual_findings(portrait_spec(COVER_7616), subtitles(boxes))) == 1


def interleaved(below: int) -> list[BBox | None]:
    """Runs of `below` frames with the line below the cover, one frame with it under the
    cover between them."""
    return [BELOW if i % (below + 1) else line_at(i) for i in range(100)]


def test_boxes_outside_between_frames_inside_are_the_models_error() -> None:
    # Three in four frames put the line below the cover, but never for two seconds: the
    # frame between them saw the line under it, and a burned-in line does not jump away
    # and back within a second and a half.
    assert checks.residual_findings(portrait_spec(), subtitles(interleaved(3))) == []
    assert len(checks.residual_findings(portrait_spec(), subtitles(interleaved(4)))) == 1


def test_a_left_over_band_counts_from_a_third_of_the_frames_that_show_text() -> None:
    assert checks.RESIDUAL_SHARE == Fraction(1, 3)
    assert len(checks.residual_findings(portrait_spec(), subtitles(frames(60, 30)))) == 1
    assert checks.residual_findings(portrait_spec(), subtitles(frames(61, 30))) == []


def test_a_short_band_is_not_enough_however_short_the_video() -> None:
    assert checks.residual_findings(portrait_spec(), subtitles(frames(3, 3))) == []
    assert len(checks.residual_findings(portrait_spec(), subtitles(frames(4, 4)))) == 1


def test_boxes_of_a_document_measured_at_another_size_are_put_in_the_videos_pixels() -> None:
    half = [
        None if b is None else BBox(x=b.x // 2, y=b.y // 2, w=b.w // 2, h=b.h // 2)
        for b in frames(60, 40)
    ]
    f = only(checks.residual_findings(portrait_spec(), subtitles(half, size=(540, 960))))
    assert "(x=210 y=1620 w=660 h=80)" in f.issue.reason


def test_nothing_is_checked_without_a_cover_or_without_frame_boxes() -> None:
    assert checks.residual_findings(portrait_spec(cover=None), subtitles(frames(10, 90))) == []
    older = subtitles(frames(10, 90)).model_copy(
        update={"frame_interval_ms": None, "frame_boxes": (), "unusable_frames": ()}
    )
    assert checks.residual_findings(portrait_spec(), older) == []


def test_intentional_omission_is_not_a_fidelity_timing_or_silence_retry():
    omitted = seg(0, zh="[unintelligible]", vi="").model_copy(
        update={"status": "unresolved", "reason": "No source evidence"}
    )
    doc = DubSegmentsDocument(
        aweme_id="7001",
        script_version=1,
        segments=(
            DubSegment(
                id=0,
                text_vi="",
                status="unresolved",
                reason=omitted.reason,
                duration_ms=0,
                stretch_ratio=1.0,
                overflow_unresolved=False,
            ),
        ),
    )
    assert checks.line_findings([omitted]) == []
    assert timing([omitted], doc, audio=Loudness(float("-inf"), float("-inf"))) == []
    assert timing([seg(1), omitted], doc, audio=Loudness(float("-inf"), float("-inf")))
