"""The OCR eval's measures, and one clip run end to end on a scripted OCR.

The numbers that matter in the real eval come from labelled real frames; here the
measures are checked on boxes whose answers are known by construction."""

import subprocess
from pathlib import Path

import ocr_eval
import pytest

from localization.contracts import BBox
from localization.providers.errors import UnusableAnswer

TEXT = BBox(x=200, y=1296, w=660, h=76)


def test_iou_of_a_box_with_itself_is_one_and_disjoint_boxes_zero() -> None:
    assert ocr_eval.iou(TEXT, TEXT) == 1.0
    assert ocr_eval.iou(TEXT, BBox(x=0, y=0, w=10, h=10)) == 0.0
    half = BBox(x=TEXT.x, y=TEXT.y, w=TEXT.w, h=TEXT.h // 2)
    assert ocr_eval.iou(TEXT, half) == pytest.approx(0.5)


def test_coverage_and_hit_tell_a_cover_that_misses_part_of_the_text() -> None:
    cover = BBox(x=150, y=1280, w=800, h=60)  # stops 32 px above the text's bottom
    assert ocr_eval.coverage(TEXT, cover) == pytest.approx((1340 - 1296) / 76)
    assert not ocr_eval.contains(cover, TEXT)
    whole = BBox(x=150, y=1270, w=800, h=120)
    assert ocr_eval.coverage(TEXT, whole) == 1.0 and ocr_eval.contains(whole, TEXT)
    assert ocr_eval.coverage(TEXT, None) == 0.0 and not ocr_eval.contains(None, TEXT)


def test_the_percentile_interpolates_and_has_none_for_nothing() -> None:
    assert ocr_eval.percentile([0.2, 0.4, 0.6, 0.8, 1.0], 0.5) == 0.6
    assert ocr_eval.percentile([0.0, 1.0], 0.1) == pytest.approx(0.1)
    assert ocr_eval.percentile([], 0.5) is None


def _frame(label, model, unusable=False) -> ocr_eval.FrameResult:  # noqa: ANN001
    return ocr_eval.score_frame(
        ocr_eval.FrameResult("c", 0, label, model, unusable, cost_usd=0.0001)
    )


def test_the_summary_counts_false_nulls_false_boxes_and_unusable_answers() -> None:
    text = [TEXT.x, TEXT.y, TEXT.w, TEXT.h]
    frames = [
        _frame(text, text),  # right
        _frame(text, None),  # missed it
        _frame(text, None, unusable=True),  # prose: unusable, not a false null
        _frame(None, text),  # saw a subtitle that is not there
        _frame(None, None),  # right
    ]
    summary = ocr_eval.summarize(frames, [])
    assert summary["unusable_rate"] == pytest.approx(1 / 5)
    assert summary["false_null_rate"] == pytest.approx(1 / 3)
    assert summary["false_box_rate"] == pytest.approx(1 / 2)
    assert summary["box_iou_median"] == 1.0
    assert summary["cost_per_frame"] == pytest.approx(0.0001)


def test_g5_needs_both_the_median_and_the_tenth_percentile() -> None:
    def frames(ious: list[float]) -> list[ocr_eval.FrameResult]:
        out = []
        for value in ious:
            f = ocr_eval.FrameResult("c", 0, [0, 0, 1, 1], [0, 0, 1, 1], False, None)
            f.iou = value
            out.append(f)
        return out

    assert ocr_eval.summarize(frames([0.8] * 9 + [0.6]), [])["g5_pass"] is True
    # A good median with a bad tail fails.
    assert ocr_eval.summarize(frames([0.9] * 8 + [0.1, 0.1]), [])["g5_pass"] is False


def test_tightness_is_a_ratio_to_the_labelled_text_so_big_text_is_not_penalised() -> None:
    """A two-line 190 px title under a 289 px cover (about 1.5) is a good cover."""
    clip = ocr_eval.ClipResult("t", [100, 1000, 880, 289], 0, 10, [], [], 0.0)
    labels = [{"at_ms": 0, "box": [120, 1050, 840, 190]}]
    scored = ocr_eval.score_clip(clip, labels, cue_start_ms=None)
    assert scored.cover_tightness == [pytest.approx(289 / 190)]
    assert scored.region_hit == [True]


def test_the_cue_start_error_is_signed() -> None:
    clip = ocr_eval.ClipResult("t", [0, 0, 10, 10], 7500, 10, [], [], 0.0)
    assert ocr_eval.score_clip(clip, [], cue_start_ms=2000).cue_start_error_ms == 5500


class ScriptedOcr:
    """Answers by frame order: the labelled frames first, then every sampled frame."""

    def __init__(self, answers: list[BBox | None | Exception]) -> None:
        self.answers = answers
        self.calls = 0

    async def detect_region(self, frame: bytes, width: int, height: int) -> BBox | None:
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_one_clip_end_to_end_keeps_raw_boxes_and_scores_the_cover(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=gray:s=320x240:d=3:r=10", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )  # fmt: skip
    line = BBox(x=40, y=180, w=240, h=20)
    ocr = ScriptedOcr([line, UnusableAnswer("prose"), None, line, line, line, line, line])
    clip = {
        "clip": "synthetic",
        "video": str(video),
        "cue_start_ms": 0,
        "frames": [
            {"at_ms": 500, "box": [40, 180, 240, 20]},
            {"at_ms": 1500, "box": [40, 180, 240, 20]},
        ],
    }
    frames, result = await ocr_eval.eval_clip(clip, ocr, ocr_eval.CostMeter(), 1024, 2.0)  # type: ignore[arg-type]
    assert [f.iou for f in frames] == [1.0, None]
    assert [f.unusable for f in frames] == [False, True]
    # 3 s at 2 frames a second, read after the two labelled ones.
    assert result.sampled_frames == len(result.raw_boxes) == 6
    assert result.raw_boxes[0] is None and result.raw_boxes[1] == [40, 180, 240, 20]
    assert result.region_hit == [True, True]
    assert result.cue_start_error_ms == 500
