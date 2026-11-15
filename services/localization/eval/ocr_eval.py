"""Measures the subtitle OCR and the cover built from it on frames a person labelled.

    # 1. frames to label, at the source's resolution
    uv run python eval/ocr_eval.py frames --video clip.mp4 --times 1000,2500,6000 --out labels/7518

    # 2. the eval, once per setting compared
    uv run python eval/ocr_eval.py run --labels labels.json --model deepseek/deepseek-v4.1-flash \
        --bbox-format xyxy_norm1000 [--max-side 1024] [--fps 2] [--reasoning off] --out report.json

Labels (JSON), boxes in source pixels drawn on the real frame (never on text painted
over a source frame, which is how the first G5 measurement went wrong):

    {"clips": [{"clip": "7518248980598639930", "video": "path/to/source.mp4",
                "cue_start_ms": 2000,
                "frames": [{"at_ms": 2000, "box": [x, y, w, h]},
                           {"at_ms": 9000, "box": null}]}]}

`cue_start_ms` (optional) is when the burned-in subtitle first shows.

Per labelled frame, the model's own box:
  box_iou            IoU with the label (G5: median >= 0.7 and p10 >= 0.5)
  unusable_rate      share of frames answered with prose, nothing, or a box outside the frame
  false_null_rate    labelled subtitle, model said none
  false_box_rate     labelled no subtitle, model gave a box
Per clip, the cover the production steps build from every sampled frame (`locate`):
  text_coverage      share of the labelled text box inside the cover (target 1.0)
  region_hit         the labelled box lies wholly inside the cover
  cover_tightness    cover height / labelled text height (target <= 2.5); a pixel ceiling
                     is wrong for big text, so it is a ratio to the label
  cue_start_error_ms first cue start minus `cue_start_ms`
  cost_per_frame, cost_per_run   from OpenRouter's `usage.cost`, never a price table

The report keeps every raw box, so a result can be rebuilt without asking the model again.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from common import load_env

from localization.activities.detect_subtitles import BATCH_SIZE, OCR_MAX_SIDE, locate, to_source
from localization.activities.ffmpeg import extract_frames, probe_video
from localization.contracts import BBox
from localization.observability.llm_usage import parse_usage
from localization.providers.errors import UnusableAnswer
from localization.providers.vision_ocr import VisionOcr

# --- metrics (pure) -----------------------------------------------------------------------


def _area(b: BBox) -> int:
    return b.w * b.h


def intersection(a: BBox, b: BBox) -> int:
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return max(0, w) * max(0, h)


def iou(a: BBox, b: BBox) -> float:
    inter = intersection(a, b)
    return inter / (_area(a) + _area(b) - inter)


def coverage(label: BBox, cover: BBox | None) -> float:
    return 0.0 if cover is None else intersection(label, cover) / _area(label)


def contains(cover: BBox | None, label: BBox) -> bool:
    return (
        cover is not None
        and cover.x <= label.x
        and cover.y <= label.y
        and label.x + label.w <= cover.x + cover.w
        and label.y + label.h <= cover.y + cover.h
    )


def percentile(values: Sequence[float], q: float) -> float | None:
    """The `q` quantile (0..1) by linear interpolation, None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


@dataclass
class FrameResult:
    clip: str
    at_ms: int
    label: list[int] | None
    model: list[int] | None
    unusable: bool
    cost_usd: float | None
    iou: float | None = None


@dataclass
class ClipResult:
    clip: str
    region: list[int] | None
    first_cue_ms: int | None
    sampled_frames: int
    raw_boxes: list[list[int] | None]
    unusable_frames: list[int]
    cost_usd: float
    text_coverage: list[float] = field(default_factory=list)
    region_hit: list[bool] = field(default_factory=list)
    cover_tightness: list[float] = field(default_factory=list)
    cue_start_error_ms: int | None = None


def _box(raw: Sequence[int] | None) -> BBox | None:
    return None if raw is None else BBox(x=raw[0], y=raw[1], w=raw[2], h=raw[3])


def _list(box: BBox | None) -> list[int] | None:
    return None if box is None else [box.x, box.y, box.w, box.h]


def score_frame(result: FrameResult) -> FrameResult:
    label, model = _box(result.label), _box(result.model)
    if label is not None and model is not None:
        result.iou = iou(label, model)
    return result


def score_clip(result: ClipResult, labels: Sequence[dict], cue_start_ms: int | None) -> ClipResult:
    region = _box(result.region)
    for frame in labels:
        label = _box(frame["box"])
        if label is None:
            continue
        result.text_coverage.append(coverage(label, region))
        result.region_hit.append(contains(region, label))
        if region is not None:
            result.cover_tightness.append(region.h / label.h)
    if cue_start_ms is not None and result.first_cue_ms is not None:
        result.cue_start_error_ms = result.first_cue_ms - cue_start_ms
    return result


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(frames: Sequence[FrameResult], clips: Sequence[ClipResult]) -> dict[str, Any]:
    ious = [f.iou for f in frames if f.iou is not None]
    with_text = [f for f in frames if f.label is not None]
    without_text = [f for f in frames if f.label is None]
    usable = [f for f in frames if not f.unusable]
    frame_costs = [f.cost_usd for f in frames if f.cost_usd is not None]
    coverage_all = [c for clip in clips for c in clip.text_coverage]
    hits = [h for clip in clips for h in clip.region_hit]
    tightness = [t for clip in clips for t in clip.cover_tightness]
    cue_errors = [abs(c.cue_start_error_ms) for c in clips if c.cue_start_error_ms is not None]
    median_iou, p10_iou = percentile(ious, 0.5), percentile(ious, 0.1)
    return {
        "frames": len(frames),
        "clips": len(clips),
        "box_iou_median": median_iou,
        "box_iou_p10": p10_iou,
        "g5_pass": median_iou is not None
        and p10_iou is not None
        and median_iou >= 0.7
        and p10_iou >= 0.5,
        "unusable_rate": sum(f.unusable for f in frames) / len(frames) if frames else None,
        "false_null_rate": (
            sum(f.model is None and not f.unusable for f in with_text) / len(with_text)
            if with_text
            else None
        ),
        "false_box_rate": (
            sum(f.model is not None for f in without_text) / len(without_text)
            if without_text
            else None
        ),
        "text_coverage_mean": _mean(coverage_all),
        "text_coverage_min": min(coverage_all) if coverage_all else None,
        "region_hit_rate": sum(hits) / len(hits) if hits else None,
        "cover_tightness_median": percentile(tightness, 0.5),
        "cover_tightness_max": max(tightness) if tightness else None,
        "cue_start_error_ms_mean_abs": _mean(cue_errors),
        "cost_per_frame": _mean(frame_costs),
        "cost_per_run": _mean([c.cost_usd for c in clips]),
        "usable_frames": len(usable),
    }


# --- running the model --------------------------------------------------------------------


class CostMeter:
    """The cost OpenRouter reports on each response the client receives."""

    def __init__(self) -> None:
        self.costs: list[float | None] = []

    async def hook(self, response: httpx.Response) -> None:
        if response.status_code != 200:
            return
        await response.aread()
        try:
            usage = parse_usage(response.json())
        except ValueError:
            usage = None
        self.costs.append(usage.cost_usd if usage else None)

    def take(self) -> list[float | None]:
        taken, self.costs = self.costs, []
        return taken


# As many requests in flight as the activity sends at once.
_LIMIT = asyncio.Semaphore(BATCH_SIZE)


async def _ask(ocr: VisionOcr, image: bytes, scaled: tuple[int, int]) -> tuple[BBox | None, bool]:
    async with _LIMIT:
        try:
            return await ocr.detect_region(image, *scaled), False
        except UnusableAnswer:
            return None, True


def _frames(video: Path, out: Path, *, start_s: float, count: int, fps: float, size) -> list[Path]:  # noqa: ANN001
    out.mkdir(parents=True, exist_ok=True)
    return extract_frames(
        video, out, start_s=start_s, count=count, fps=fps, width=size[0], height=size[1]
    )


async def eval_clip(
    clip: dict, ocr: VisionOcr, meter: CostMeter, max_side: int, fps: float
) -> tuple[list[FrameResult], ClipResult]:
    video = Path(clip["video"])
    info = probe_video(video)
    source = (info.width, info.height)
    factor = min(1.0, max_side / max(source))
    scaled = (max(1, round(source[0] * factor)), max(1, round(source[1] * factor)))
    frames: list[FrameResult] = []
    with tempfile.TemporaryDirectory(prefix="ocr-eval-") as tmp:
        for n, labelled in enumerate(clip["frames"]):
            (path,) = _frames(
                video, Path(tmp) / f"l{n}", start_s=labelled["at_ms"] / 1000, count=1, fps=fps,
                size=scaled,
            )  # fmt: skip
            box, bad = await _ask(ocr, path.read_bytes(), scaled)
            costs = meter.take()
            frames.append(
                score_frame(
                    FrameResult(
                        clip=clip["clip"],
                        at_ms=labelled["at_ms"],
                        label=labelled["box"],
                        model=_list(to_source(box, scaled, source)),
                        unusable=bad,
                        cost_usd=costs[0] if costs else None,
                    )
                )
            )
        count = max(1, int(info.duration_ms / 1000 * fps) + 1)
        sampled = _frames(video, Path(tmp) / "all", start_s=0, count=count, fps=fps, size=scaled)
        answers = await asyncio.gather(*(_ask(ocr, p.read_bytes(), scaled) for p in sampled))
    boxes = [to_source(box, scaled, source) for box, _ in answers]
    region, cues = locate(boxes, info.width, info.height, info.duration_ms, round(1000 / fps))
    clip_result = ClipResult(
        clip=clip["clip"],
        region=_list(region),
        first_cue_ms=cues[0].start_ms if cues else None,
        sampled_frames=len(sampled),
        raw_boxes=[_list(b) for b in boxes],
        unusable_frames=[i for i, (_, bad) in enumerate(answers) if bad],
        cost_usd=sum(c for c in meter.take() if c is not None),
    )
    return frames, score_clip(clip_result, clip["frames"], clip.get("cue_start_ms"))


async def run(args: argparse.Namespace) -> int:
    load_env()
    meter = CostMeter()
    http = httpx.AsyncClient(timeout=120, event_hooks={"response": [meter.hook]})
    ocr = VisionOcr(
        os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        os.environ["OPENROUTER_API_KEY"],
        args.model,
        args.bbox_format,
        http=http,
        answer_only=args.reasoning == "off",
    )
    labels = json.loads(Path(args.labels).read_text())
    frames: list[FrameResult] = []
    clips: list[ClipResult] = []
    try:
        for clip in labels["clips"]:
            clip_frames, clip_result = await eval_clip(clip, ocr, meter, args.max_side, args.fps)
            frames += clip_frames
            clips.append(clip_result)
            print(f"{clip['clip']}: region {clip_result.region}, hit {clip_result.region_hit}")
    finally:
        await http.aclose()
    setting = {
        "model": args.model,
        "bbox_format": args.bbox_format,
        "max_side": args.max_side,
        "fps": args.fps,
        "reasoning": args.reasoning,
    }
    summary = summarize(frames, clips)
    report = {
        "setting": setting,
        "summary": summary,
        "frames": [asdict(f) for f in frames],
        "clips": [asdict(c) for c in clips],
    }
    Path(args.out).write_text(json.dumps(report, indent=2))
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


def frames_for_labels(args: argparse.Namespace) -> int:
    video = Path(args.video)
    info = probe_video(video)
    out = Path(args.out)
    for at in (int(t) for t in args.times.split(",")):
        with tempfile.TemporaryDirectory() as tmp:
            (path,) = _frames(
                video, Path(tmp), start_s=at / 1000, count=1, fps=1,
                size=(info.width, info.height),
            )  # fmt: skip
            out.mkdir(parents=True, exist_ok=True)
            target = out / f"{at:06d}ms.jpg"
            target.write_bytes(path.read_bytes())
            print(target)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    frames = sub.add_parser("frames", help="extract frames to label")
    frames.add_argument("--video", required=True)
    frames.add_argument("--times", required=True, help="comma-separated ms")
    frames.add_argument("--out", required=True)
    evaluate = sub.add_parser("run", help="run the eval on labelled frames")
    evaluate.add_argument("--labels", required=True)
    evaluate.add_argument("--model", required=True)
    evaluate.add_argument(
        "--bbox-format",
        required=True,
        choices=("xyxy_px", "xyxy_norm1", "xyxy_norm1000", "yxyx_norm1000"),
    )
    evaluate.add_argument("--max-side", type=int, default=OCR_MAX_SIDE)
    evaluate.add_argument("--fps", type=float, default=2.0)
    evaluate.add_argument("--reasoning", choices=("off", "on"), default="off")
    evaluate.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.command == "frames":
        return frames_for_labels(args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
