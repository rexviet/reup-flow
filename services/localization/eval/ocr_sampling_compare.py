"""Measures what a cheaper subtitle detection loses, on one finished run: fewer frames a
second (`detect_subtitles` samples 2) and smaller frames (it sends 1024 px on the long side).

    uv run python eval/ocr_sampling_compare.py \\
        --video ../../data/review/<aweme>/g3/<sha>.mp4 \\
        --reference ../../data/ocr-bench/<aweme>-subtitles.json \\
        --model deepseek/deepseek-v4.1-flash --bbox-format xyxy_norm1000 \\
        --max-side 1024,768,512 --out ../../data/ocr-bench/<aweme>-sampling

The reference is the production run's subtitle document: its per-frame boxes, its band
and its cues. Nothing here is a human label, so "agrees" means "agrees with production".
Every frame is asked again at 2 fps for each `--max-side`; 1 fps is the even frames of
those same answers (the frames at whole seconds), so it costs nothing more. Asking again
at 1024 px measures how much the model disagrees with itself, the floor every other
setting is read against. Each setting's answers are saved to `<out>/<max_side>.json` and
not asked again on a rerun.

Per setting it prints: the cost, the frames whose answer was unusable, agreement on
whether a frame shows a subtitle, the IoU of the boxes where both saw one, the band (as
`locate` builds it) against the reference band, and the cues against the reference cues:
the share of subtitle time both cover, missed and extra cues, and the start and end
error of each reference cue against the cue that overlaps it most.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
from common import load_env
from ocr_eval import CostMeter, iou, percentile

from localization.activities.detect_subtitles import FRAME_ATTEMPTS, locate, to_source
from localization.activities.ffmpeg import extract_frames, probe_video
from localization.contracts import BBox, SubtitleCue, SubtitleDocument
from localization.providers.errors import TransientProviderError, UnusableAnswer
from localization.providers.vision_ocr import VisionOcr

SAMPLE_FPS = 2
CHUNK = 200


def _scaled(source: tuple[int, int], max_side: int) -> tuple[int, int]:
    factor = min(1.0, max_side / max(source))
    return max(1, round(source[0] * factor)), max(1, round(source[1] * factor))


async def _read(
    ocr: VisionOcr, limit: asyncio.Semaphore, image: bytes, scaled: tuple[int, int]
) -> tuple[BBox | None, bool]:
    async with limit:
        for attempt in range(1, FRAME_ATTEMPTS + 1):
            try:
                return await ocr.detect_region(image, *scaled), False
            except UnusableAnswer:
                return None, True
            except TransientProviderError:
                if attempt == FRAME_ATTEMPTS:
                    raise
                await asyncio.sleep(2.0 * attempt)
    raise AssertionError("unreachable")


async def ask_all(
    ocr: VisionOcr, meter: CostMeter, video: Path, source: tuple[int, int], frames: int,
    max_side: int, concurrency: int,
) -> dict[str, Any]:  # fmt: skip
    """Every frame at 2 fps, read at `max_side`, as the production activity reads it."""
    scaled = _scaled(source, max_side)
    limit = asyncio.Semaphore(concurrency)
    boxes: list[BBox | None] = []
    unusable: list[int] = []
    with tempfile.TemporaryDirectory(prefix="ocr-sampling-") as tmp:
        while len(boxes) < frames:
            out = Path(tmp) / f"c{len(boxes)}"
            out.mkdir()
            paths = extract_frames(
                video, out, start_s=len(boxes) / SAMPLE_FPS,
                count=min(CHUNK, frames - len(boxes)), fps=SAMPLE_FPS,
                width=scaled[0], height=scaled[1], at_end=bool(boxes),
            )  # fmt: skip
            if not paths:
                break
            found = await asyncio.gather(
                *(_read(ocr, limit, p.read_bytes(), scaled) for p in paths)
            )
            unusable += [len(boxes) + i for i, (_, bad) in enumerate(found) if bad]
            boxes += [to_source(box, scaled, source) for box, _ in found]
            print(f"  {max_side}px: {len(boxes)}/{frames}", flush=True)
    costs = meter.take()
    return {
        "max_side": max_side,
        "scaled": list(scaled),
        "boxes": [None if b is None else [b.x, b.y, b.w, b.h] for b in boxes],
        "unusable": unusable,
        "cost_usd": sum(c for c in costs if c is not None),
        "priced_calls": sum(c is not None for c in costs),
    }


# --- comparison (pure) --------------------------------------------------------------------


def _box(raw: list[int] | None) -> BBox | None:
    return None if raw is None else BBox(x=raw[0], y=raw[1], w=raw[2], h=raw[3])


def _overlap_ms(a: SubtitleCue, b: SubtitleCue) -> int:
    return max(0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))


def _covered_ms(cues: tuple[SubtitleCue, ...]) -> set[int]:
    """Subtitle time as a set of 100 ms ticks: exact enough at 500 ms frames."""
    return {t for c in cues for t in range(c.start_ms // 100, c.end_ms // 100)}


def compare(
    ref_boxes: list[BBox | None], ref_unusable: set[int], ref_region: BBox | None,
    ref_cues: tuple[SubtitleCue, ...], boxes: list[BBox | None], unusable: set[int],
    step: int, width: int, height: int, duration_ms: int,
) -> dict[str, Any]:  # fmt: skip
    """One setting against the reference. `step` 2 keeps every other frame (1 fps)."""
    picked = range(0, min(len(ref_boxes), len(boxes)), step)
    both_usable = [i for i in picked if i not in ref_unusable and i not in unusable]
    agree = sum((ref_boxes[i] is None) == (boxes[i] is None) for i in both_usable)
    only_ref = sum(ref_boxes[i] is not None and boxes[i] is None for i in both_usable)
    only_new = sum(ref_boxes[i] is None and boxes[i] is not None for i in both_usable)
    ious = [iou(ref_boxes[i], boxes[i]) for i in both_usable if ref_boxes[i] and boxes[i]]  # type: ignore[arg-type]
    region, cues = locate(
        [boxes[i] for i in picked], width, height, duration_ms, interval_ms=500 * step
    )
    ref_time, new_time = _covered_ms(ref_cues), _covered_ms(cues)
    start_err, end_err, missed = [], [], 0
    for ref in ref_cues:
        best = max(cues, key=lambda c: _overlap_ms(ref, c), default=None)
        if best is None or _overlap_ms(ref, best) == 0:
            missed += 1
            continue
        start_err.append(abs(best.start_ms - ref.start_ms))
        end_err.append(abs(best.end_ms - ref.end_ms))
    extra = sum(all(_overlap_ms(c, ref) == 0 for ref in ref_cues) for c in cues)
    return {
        "frames": len(picked),
        "unusable": sum(i in unusable for i in picked),
        "presence_agreement": agree / len(both_usable) if both_usable else None,
        "subtitle_only_in_reference": only_ref,
        "subtitle_only_in_setting": only_new,
        "box_iou_median": percentile(ious, 0.5),
        "box_iou_p10": percentile(ious, 0.1),
        "region": None if region is None else [region.x, region.y, region.w, region.h],
        "region_iou": iou(ref_region, region) if ref_region and region else None,
        "cues": len(cues),
        "cue_time_iou": len(ref_time & new_time) / len(ref_time | new_time)
        if ref_time | new_time
        else None,
        "cues_missed": missed,
        "cues_extra": extra,
        "cue_start_error_ms_median": percentile(start_err, 0.5),
        "cue_start_error_ms_p90": percentile(start_err, 0.9),
        "cue_end_error_ms_median": percentile(end_err, 0.5),
        "cue_end_error_ms_p90": percentile(end_err, 0.9),
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


async def run(args: argparse.Namespace) -> int:
    load_env()
    video = Path(args.video)
    info = probe_video(video)
    doc = SubtitleDocument.model_validate_json(Path(args.reference).read_text())
    if (doc.width, doc.height) != (info.width, info.height):
        raise SystemExit("the reference was made on a video of another size")
    if doc.frame_interval_ms != 500:
        raise SystemExit("the reference was not sampled at 2 fps")
    source = (info.width, info.height)
    ref_boxes = list(doc.frame_boxes)
    ref_unusable = set(doc.unusable_frames)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    meter = CostMeter()
    http = httpx.AsyncClient(timeout=120, event_hooks={"response": [meter.hook]})
    ocr = VisionOcr(
        os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        os.environ["OPENROUTER_API_KEY"],
        args.model,
        args.bbox_format,
        http=http,
    )
    answers: dict[int, dict[str, Any]] = {}
    try:
        for side in (int(s) for s in args.max_side.split(",")):
            saved = out / f"{side}.json"
            if saved.exists():
                answers[side] = json.loads(saved.read_text())
                continue
            print(f"asking {len(ref_boxes)} frames at {side}px", flush=True)
            answers[side] = await ask_all(
                ocr, meter, video, source, len(ref_boxes), side, args.concurrency
            )
            saved.write_text(json.dumps(answers[side]))
    finally:
        await http.aclose()

    settings: list[tuple[str, list[BBox | None], set[int], int, float | None]] = [
        ("production 1024px 2fps", ref_boxes, ref_unusable, 1, None),
        ("production 1024px 1fps", ref_boxes, ref_unusable, 2, None),
    ]
    for side, answer in answers.items():
        boxes = [_box(b) for b in answer["boxes"]]
        for step, fps in ((1, 2), (2, 1)):
            cost = answer["cost_usd"] * (len(range(0, len(boxes), step)) / max(1, len(boxes)))
            name = f"rerun {side}px {fps}fps"
            settings.append((name, boxes, set(answer["unusable"]), step, cost))
    rows = []
    for name, boxes, unusable, step, cost in settings:
        row = {"setting": name, "cost_usd": cost}
        row |= compare(
            ref_boxes, ref_unusable, doc.region, doc.cues, boxes, unusable,
            step, info.width, info.height, info.duration_ms,
        )  # fmt: skip
        rows.append(row)
    report = {
        "video": str(video),
        "reference": {
            "region": None if doc.region is None else doc.region.model_dump(),
            "cues": len(doc.cues),
            "frames": len(ref_boxes),
            "unusable": len(ref_unusable),
        },
        "answers": {
            side: {k: a[k] for k in ("cost_usd", "priced_calls", "scaled")}
            for side, a in answers.items()
        },
        "rows": rows,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2))
    keys = [k for k in rows[0] if k != "setting"]
    print("\nsetting".ljust(26) + "".join(k[:14].rjust(15) for k in keys))
    for row in rows:
        print(row["setting"].ljust(25) + "".join(_fmt(row[k]).rjust(15) for k in keys))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--video", required=True, help="the source video of the reference run")
    parser.add_argument("--reference", required=True, help="the run's subtitles.json")
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--bbox-format",
        required=True,
        choices=("xyxy_px", "xyxy_norm1", "xyxy_norm1000", "yxyx_norm1000"),
    )
    parser.add_argument("--max-side", default="1024,768,512", help="comma-separated px")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--out", required=True)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
