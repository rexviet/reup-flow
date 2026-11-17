"""Runs a local DeepSeek-OCR the way production's `detect_subtitles` runs, over the videos
whose review was approved and rendered, and compares what it finds with what production
found on the same videos (OpenRouter `OCR_MODEL`, today `deepseek/deepseek-v4.1-flash`).

Production, for each video: every frame at `SAMPLE_FPS` (2 fps), scaled to
`OCR_MAX_SIDE` (1024 px), extracted a chunk at a time by the same `extract_frames`; one
box per frame from the vision model, mapped back to source pixels by `to_source`; then
`locate` turns the boxes into the subtitle region and its cues. This bench does all of
that with production's own functions, and swaps only the model.

Arms, each a local model and the request it is sent, all on the same frames:

    qwen8b-production    Qwen3-VL-8B with production's own request: `vision_ocr.PROMPT`,
                         its `max_tokens`, its reply parsing (JSON `bbox` in
                         `OCR_BBOX_FORMAT`), read from the code by `prepare`, so the
                         bench cannot drift from what the workflow sends. The baseline a
                         prompt optimisation starts from.
    qwen8b-gepa          The same, with the prompt `ocr_prompt_gepa.py` wrote into
                         `arms.json` as the `gepa` request.
    deepseek-production  DeepSeek-OCR with the same request. It answers nothing: the
                         model follows no instruction but its own task prompts.
    deepseek-grounding   DeepSeek-OCR with its task prompt `<|grounding|>OCR this image.`:
                         every text line with its box (0..999 of each side). The adapter
                         that stands in for `detect_region` takes the lowest run of
                         centred lines (the production rule, `MAX_CENTRE_OFFSET`).

The model runs in mlx-vlm's environment, not the project's, so there are three steps;
`run` does all three (and skips the work already done, so it resumes):

    uv run python eval/local_ocr_dataset_bench.py run --limit 10 \\
        --out ../../data/ocr-bench/dataset-v1

    # or one at a time
    uv run python eval/local_ocr_dataset_bench.py prepare --limit 10 --out ...
    uv run --no-project --with mlx-vlm==0.7.12 \\
        python eval/local_ocr_dataset_bench.py infer --arm qwen8b-production --out ...
    uv run python eval/local_ocr_dataset_bench.py score --out ...

Nothing here calls OpenRouter: the reference is the subtitle document production already
stored for the approved generation (`work/<id>/g<n>/r0/subtitles.json`), read from MinIO.
It is a model's answer, not a human label, so "agreement" means "agrees with production".

mlx-vlm 0.7.12 loads DeepSeek-OCR only with its processor module imported first (that
registers the processor) and `trust_remote_code=False` (the repository's own PyTorch code
needs an older transformers than mlx-vlm's).
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

DEEPSEEK_OCR = "mlx-community/DeepSeek-OCR-8bit"
QWEN_8B = "mlx-community/Qwen3-VL-8B-Instruct-4bit"
MLX_VLM = "mlx-vlm==0.7.12"
# arm -> (local model, request in arms.json, reply parser)
ARMS = {
    "qwen8b-production": (QWEN_8B, "production", "production"),
    # production's request with the prompt `ocr_prompt_gepa.py` found
    "qwen8b-gepa": (QWEN_8B, "gepa", "production"),
    "deepseek-production": (DEEPSEEK_OCR, "production", "production"),
    "deepseek-grounding": (DEEPSEEK_OCR, "grounding", "grounding"),
}
# A grounding reply lists a few lines of text; a runaway one is cut off here.
GROUNDING = {"prompt": "<|grounding|>OCR this image.", "max_tokens": 800}
# DeepSeek-OCR's boxes are in 0..999 of each side of the image it was given.
GRID = 999
_LINE = re.compile(r"<\|ref\|>(.*?)<\|/ref\|><\|det\|>\[\[(\d+), (\d+), (\d+), (\d+)\]\]<\|/det\|>")
# The run-to-run noise of production itself: the same OpenRouter model asked twice about
# the same frames gave subtitle regions with this IoU (two videos, 15/11).
OPENROUTER_REPEAT_REGION_IOU = (0.87, 0.92)


# --- prepare: the project's environment ---------------------------------------------

# Frames production extracted at once when it read every frame at 2 fps; this script
# reproduces that sampling, which production no longer uses.
CHUNK_FRAMES = 32


def prepare(args: argparse.Namespace) -> int:
    """Picks the approved, rendered videos, downloads each one's source and production's
    subtitle document, and extracts the frames production sent to the model."""
    import os
    from functools import partial

    import psycopg
    from common import host_database_url, load_env

    from localization.activities.detect_subtitles import SAMPLE_FPS, ocr_size
    from localization.activities.ffmpeg import extract_frames, probe_video
    from localization.config import Settings
    from localization.contracts import SubtitleDocument
    from localization.providers.vision_ocr import _ANSWER_ONLY, PROMPT
    from localization.storage.object_store import ObjectStore

    load_env()
    os.environ["S3_ENDPOINT"] = "http://127.0.0.1:9000"
    settings = Settings()
    # What `infer` sends, written here because `infer` cannot import the project.
    production = {
        "prompt": PROMPT,
        "max_tokens": _ANSWER_ONLY["max_tokens"],
        "bbox_format": settings.ocr_bbox_format,
        "openrouter_model": settings.ocr_model,
    }
    (args.out / "arms.json").write_text(
        json.dumps({"production": production, "grounding": GROUNDING}, indent=1)
    )
    store = ObjectStore.from_settings(settings)
    database = host_database_url(settings.loc_database_url.get_secret_value(), args.db_host)
    with psycopg.connect(database) as db:
        rows = db.execute(
            "SELECT aweme_id, generation FROM localization.final_renders "
            "WHERE state = 'rendered' ORDER BY rendered_at DESC, aweme_id LIMIT %s",
            (args.limit,),
        ).fetchall()
    print(f"{len(rows)} approved, rendered videos", flush=True)
    for aweme, generation in rows:
        root = args.out / aweme
        done = root / "video.json"
        if done.exists():
            continue
        frames_dir = root / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"work/{aweme}/g{generation}/r0"
        reference = store.get_bytes(f"{prefix}/subtitles.json", produced_by="detect_subtitles")
        doc = SubtitleDocument.model_validate_json(reference)
        (root / "reference.json").write_bytes(reference)
        video = root / "source.mp4"
        store.download_file(f"{prefix}/source.mp4", video, produced_by="ingest")
        info = probe_video(video)
        scaled = ocr_size(info.width, info.height)
        # The loop of `DetectSubtitlesActivity.detect_subtitles`, kept to the same seeks so
        # the frames are the same pictures; each chunk is renamed to its frame index.
        count = 0
        chunk_dir = root / "chunk"
        chunk_dir.mkdir(exist_ok=True)
        while True:
            chunk = partial(
                extract_frames, video, chunk_dir, start_s=count / SAMPLE_FPS,
                count=CHUNK_FRAMES, fps=SAMPLE_FPS, width=scaled[0], height=scaled[1],
                at_end=bool(count),
            )()  # fmt: skip
            for path in chunk:
                path.rename(frames_dir / f"{count:06d}.jpg")
                count += 1
            if len(chunk) < CHUNK_FRAMES:
                break
        chunk_dir.rmdir()
        video.unlink()  # the frames are what the model reads; the source is not kept
        done.write_text(
            json.dumps(
                {
                    "aweme_id": aweme,
                    "generation": generation,
                    "width": info.width,
                    "height": info.height,
                    "duration_ms": info.duration_ms,
                    "scaled": scaled,
                    "frames": count,
                    "reference_frames": len(doc.frame_boxes),
                }
            )
        )
        reference_frames = len(doc.frame_boxes)
        print(f"{aweme} g{generation}: {count} frames (reference {reference_frames})", flush=True)
    return 0


# --- infer: mlx-vlm's environment, standard library only otherwise --------------------


def infer(args: argparse.Namespace) -> int:
    """Asks the model about every frame. One JSON line per frame, appended as it is
    answered, so a stopped run resumes where it stopped."""
    model_id, request_name, _ = ARMS[args.arm]
    if model_id == DEEPSEEK_OCR:
        import mlx_vlm.models.deepseekocr.processing_deepseekocr  # noqa: F401 - registers it
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template

    request = json.loads((args.out / "arms.json").read_text())[request_name]
    started = time.time()
    model, processor = load(model_id, trust_remote_code=False)
    print(f"loaded {model_id} in {time.time() - started:.1f} s; arm {args.arm}", flush=True)
    prompt = apply_chat_template(processor, model.config, request["prompt"], num_images=1)
    for meta in sorted(args.out.glob("*/video.json")):
        root = meta.parent
        frames = sorted((root / "frames").glob("*.jpg"))
        answers = root / f"answers-{args.arm}.jsonl"
        done = sum(1 for _ in answers.open()) if answers.exists() else 0
        if done >= len(frames):
            continue
        print(f"{root.name}: frames {done}..{len(frames) - 1}", flush=True)
        with answers.open("a") as out:
            for index, path in enumerate(frames[done:], start=done):
                t = time.perf_counter()
                # No temperature: production sends none either.
                reply = generate(model, processor, prompt, [str(path)],
                                 max_tokens=request["max_tokens"], verbose=False)  # fmt: skip
                seconds = time.perf_counter() - t
                text = getattr(reply, "text", reply)
                out.write(json.dumps({"index": index, "seconds": seconds, "reply": text},
                                     ensure_ascii=False) + "\n")  # fmt: skip
                out.flush()
                if index % 100 == 0:
                    print(f"  {root.name} frame {index}/{len(frames)}  {seconds:.2f} s", flush=True)
    return 0


# --- score: the project's environment ------------------------------------------------


def production_box(reply: str, meta: dict, bbox_format: str):
    """What `VisionOcr.detect_region` makes of the reply, then `to_source`: the box in
    source pixels or None, and whether the answer was unusable."""
    from localization.activities.detect_subtitles import to_source
    from localization.providers.bbox import normalize_bbox
    from localization.providers.errors import PermanentProviderError
    from localization.providers.vision_ocr import _FENCE_RE

    try:
        raw = json.loads(_FENCE_RE.sub("", reply.strip()))["bbox"]
    except (KeyError, TypeError, AttributeError, ValueError):
        return None, True
    if raw is None:
        return None, False
    scaled = tuple(meta["scaled"])
    try:
        box = normalize_bbox(raw, bbox_format, *scaled)
    except PermanentProviderError:
        return None, True
    return to_source(box, scaled, (meta["width"], meta["height"])), False


def grounding_box(reply: str, meta: dict, _bbox_format: str):
    """What `detect_region` would answer, from DeepSeek-OCR's lines: the lowest run of
    centred lines, in source pixels; None when no line is centred. The second value says
    the reply was unusable: text, but no line in the grounding format."""
    from localization.activities.detect_subtitles import _centred
    from localization.contracts import BBox

    width, height = meta["width"], meta["height"]
    lines = []
    for _, *corners in _LINE.findall(reply):
        x1, y1, x2, y2 = (int(v) for v in corners)
        x, y = round(x1 * width / GRID), round(y1 * height / GRID)
        w = max(1, min(width - x, round(x2 * width / GRID) - x))
        h = max(1, min(height - y, round(y2 * height / GRID) - y))
        if x < width and y < height:
            lines.append(BBox(x=x, y=y, w=w, h=h))
    if not lines:
        return None, bool(reply.strip())
    band = []
    for box in sorted((b for b in lines if _centred(b, width)), key=lambda b: -b.y):
        if not band or band[-1].y - (box.y + box.h) <= band[-1].h:
            band.append(box)
    if not band:
        return None, False
    x1, y1 = min(b.x for b in band), min(b.y for b in band)
    x2, y2 = max(b.x + b.w for b in band), max(b.y + b.h for b in band)
    return BBox(x=x1, y=y1, w=x2 - x1, h=y2 - y1), False


PARSERS = {"production": production_box, "grounding": grounding_box}


def iou(a, b) -> float:
    ix = max(0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union else 0.0


def cue_overlap(mine, theirs) -> float | None:
    """Time-IoU of the subtitle cues: the milliseconds both put a subtitle on, over the
    milliseconds either does. None when neither has any."""

    def spans(cues):
        return [(c.start_ms, c.end_ms) for c in cues]

    def total(s):
        return sum(e - b for b, e in s)

    inter = sum(
        max(0, min(e1, e2) - max(b1, b2)) for b1, e1 in spans(mine) for b2, e2 in spans(theirs)
    )
    union = total(spans(mine)) + total(spans(theirs)) - inter
    return inter / union if union else None


def _xywh(box) -> list[int] | None:
    return [box.x, box.y, box.w, box.h] if box else None


def score_arm(arm: str, out: Path, bbox_format: str) -> dict | None:
    from localization.activities.detect_subtitles import MIN_FRAMES_TO_JUDGE, locate
    from localization.contracts import SubtitleDocument

    model_id, request_name, parser = ARMS[arm]
    parse = PARSERS[parser]
    rows = []
    for meta_path in sorted(out.glob("*/video.json")):
        root = meta_path.parent
        meta = json.loads(meta_path.read_text())
        answers_path = root / f"answers-{arm}.jsonl"
        if not answers_path.exists():
            continue
        answers = [json.loads(line) for line in answers_path.read_text().splitlines()]
        complete = len(answers) == meta["frames"]
        ref = SubtitleDocument.model_validate_json((root / "reference.json").read_text())
        width, height, duration = meta["width"], meta["height"], meta["duration_ms"]
        boxes, unusable = [], 0
        for a in answers:
            box, bad = parse(a["reply"], meta, bbox_format)
            boxes.append(box)
            unusable += bad
        region, cues = locate(boxes, width, height, duration if complete else 0)
        ref_boxes = list(ref.frame_boxes[: len(boxes)])
        if complete:
            ref_region, ref_cues = ref.region, ref.cues
        else:  # a partial run is judged on the frames it has, against the same frames
            ref_region, ref_cues = locate(ref_boxes, width, height, 0)
        paired = list(zip(boxes, ref_boxes, strict=False))
        agree = sum((m is None) == (r is None) for m, r in paired)
        ious = [iou(m, r) for m, r in paired if m and r]
        seconds = [a["seconds"] for a in answers]
        overlap = cue_overlap(cues, ref_cues)
        rows.append(
            {
                "aweme_id": meta["aweme_id"],
                "generation": meta["generation"],
                "size": [width, height],
                "duration_ms": duration,
                "complete": complete,
                "frames": len(answers),
                "frames_total": meta["frames"],
                "frames_match_reference": meta["frames"] == meta["reference_frames"],
                "s_per_frame_median": round(statistics.median(seconds), 3),
                "minutes": round(sum(seconds) / 60, 1),
                "unusable": unusable,
                # production raises PermanentProviderError on such a video
                "would_fail_in_production": len(boxes) >= MIN_FRAMES_TO_JUDGE
                and unusable * 2 > len(boxes),
                "frames_with_box": sum(b is not None for b in boxes),
                "reference_frames_with_box": sum(b is not None for b in ref_boxes),
                "presence_agreement": round(agree / len(paired), 3) if paired else None,
                "box_iou_median": round(statistics.median(ious), 3) if ious else None,
                "region": _xywh(region),
                "region_reference": _xywh(ref_region),
                "region_iou": round(iou(region, ref_region), 3)
                if region and ref_region else None,
                "region_both_none": region is None and ref_region is None,
                "cue_time_iou": None if overlap is None else round(overlap, 3),
                "cues": [[c.start_ms, c.end_ms] for c in cues],
                "cues_reference": [[c.start_ms, c.end_ms] for c in ref_cues],
                "sample_replies": [answers[i]["reply"][:200]
                                   for i in range(0, len(answers), max(1, len(answers) // 4))],
            }
        )  # fmt: skip
    if not rows:
        return None
    region_ious = [r["region_iou"] or 0.0 for r in rows if not r["region_both_none"]]
    cue_ious = [r["cue_time_iou"] for r in rows if r["cue_time_iou"] is not None]
    frames = sum(r["frames"] for r in rows)
    summary = {
        "videos": len(rows),
        "videos_complete": sum(r["complete"] for r in rows),
        "frames": frames,
        "hours": round(sum(r["minutes"] for r in rows) / 60, 2),
        "s_per_frame_median": round(statistics.median(r["s_per_frame_median"] for r in rows), 3),
        "unusable_share": round(sum(r["unusable"] for r in rows) / frames, 3),
        "videos_failing_in_production": sum(r["would_fail_in_production"] for r in rows),
        "presence_agreement_median": round(
            statistics.median(r["presence_agreement"] for r in rows), 3),
        "region_iou_median": round(statistics.median(region_ious), 3) if region_ious else None,
        "region_iou_min": round(min(region_ious), 3) if region_ious else None,
        "videos_region_iou_below_0_5": sum(i < 0.5 for i in region_ious),
        "cue_time_iou_median": round(statistics.median(cue_ious), 3) if cue_ious else None,
    }  # fmt: skip
    return {"summary": summary, "videos": rows}


def score(args: argparse.Namespace) -> int:
    from localization.activities.detect_subtitles import FRAME_INTERVAL_MS

    arms = json.loads((args.out / "arms.json").read_text())
    report = {
        "arms_models": {arm: ARMS[arm][0] for arm in ARMS},
        "frame_interval_ms": FRAME_INTERVAL_MS,
        "requests": arms,
        "openrouter_repeat_region_iou": OPENROUTER_REPEAT_REGION_IOU,
        "arms": {},
    }
    for arm in ARMS:
        result = score_arm(arm, args.out, arms["production"]["bbox_format"])
        if result:
            result["summary"] |= {"model": ARMS[arm][0], "request": ARMS[arm][1]}
            report["arms"][arm] = result
            print(arm, json.dumps(result["summary"], indent=1), flush=True)
    if not report["arms"]:
        raise SystemExit(f"nothing answered yet under {args.out}")
    (args.out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return 0


# --- run: all three ------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    prepare(args)
    here = Path(__file__).resolve()
    subprocess.run(
        ["uv", "run", "--no-project", "--with", MLX_VLM, "python", str(here), "infer",
         "--arm", args.arm, "--out", str(args.out)],
        check=True,
    )  # fmt: skip
    return score(args)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="step", required=True)
    for name in ("prepare", "infer", "score", "run"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=Path, required=True, help="the bench's directory")
        p.add_argument("--limit", type=int, default=10, help="videos, newest render first")
        p.add_argument("--db-host", default="localhost:5433", help="Postgres as the host sees it")
        p.add_argument("--arm", choices=list(ARMS), default="qwen8b-production",
                       help="infer/run: the local model and the request it is sent")  # fmt: skip
    args = parser.parse_args(argv)
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    return {"prepare": prepare, "infer": infer, "score": score, "run": run}[args.step](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
