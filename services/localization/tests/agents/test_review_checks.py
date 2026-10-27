"""The review's checks that need no model. Each has a case that fails and one that passes,
and the issues come back in the shape the workflow routes on."""

import pytest

from localization.activities.layout_captions import FontMeter
from localization.agents import review_checks as checks
from localization.agents.reviewer import Loudness, ResidualFinding
from localization.config import DEFAULT_CAPTION_FONT
from localization.contracts import (
    BBox,
    Caption,
    Cover,
    DubSegment,
    DubSegmentsDocument,
    EditSpec,
    LocalizedSegment,
)

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
    fast = only(timing([seg(0)], dub((0, 1900, 1.13, False))))
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


def test_the_ocr_is_asked_about_captions_spread_over_the_video_and_not_without_a_cover() -> None:
    many = tuple(caption(i, "x", start=i * 1000) for i in range(10))
    times = checks.residual_times(spec_with(*many))
    assert len(times) == checks.RESIDUAL_FRAMES and times == sorted(times)
    assert all(t in {c.start_ms + 500 for c in many} for t in times)
    assert checks.residual_times(spec_with(*many, cover=False)) == []
    assert len(checks.residual_times(spec_with())) == checks.RESIDUAL_FRAMES


def test_source_text_just_above_the_caption_is_left_over_subtitle() -> None:
    spec = spec_with()
    f = only(checks.residual_findings(spec, [ResidualFinding(500, (30, 330, 200, 40))]))
    assert (f.dimension, f.issue.stage, f.issue.segment_id) == ("visual", "render", None)
    assert "500 ms" in f.issue.reason


def test_the_same_leftover_seen_on_several_frames_is_one_issue() -> None:
    seen = [ResidualFinding(t, (30, 330, 200, 40)) for t in (500, 2500, 4500)]
    f = only(checks.residual_findings(spec_with(), seen))
    assert "500, 2500, 4500 ms" in f.issue.reason


def test_text_in_the_caption_place_far_away_or_unreadable_is_not_a_finding() -> None:
    spec = spec_with()
    found = [
        ResidualFinding(0, (30, 390, 200, 40)),  # the caption itself
        ResidualFinding(1, (10, 20, 120, 30)),  # a title far from the caption
        ResidualFinding(2, None),
        ResidualFinding(3, None, unavailable=True),
    ]
    assert checks.residual_findings(spec, found) == []
