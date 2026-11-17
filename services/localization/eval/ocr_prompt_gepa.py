"""Optimises the subtitle-band prompt of `detect_subtitles` for a local vision model with
GEPA (https://github.com/gepa-ai/gepa), then hands the result to
`local_ocr_dataset_bench.py` as the request of its `qwen8b-gepa` arm.

The frames come from the approved, rendered videos that the bench does *not* use, so
the prompt is never tuned on the videos it is judged on. Each is a frame production
sampled (2 fps, 1024 px, the same `extract_frames` chunks), with production's answer on
it as the target. Everything else is production's: the model's reply is parsed as
`VisionOcr.detect_region` parses it (JSON `bbox` in `OCR_BBOX_FORMAT`, `max_tokens`
from `vision_ocr`), and a box only counts as a subtitle when `locate` would keep it
(centred, `MAX_CENTRE_OFFSET`).

    # 1. the frames (project environment; reads MinIO and Postgres)
    uv run python eval/ocr_prompt_gepa.py data --bench ../../data/ocr-bench/dataset-v1 \\
        --out ../../data/ocr-bench/gepa-v1

    # 2. optimise (the local model, GEPA, and Claude Code as the reflection model)
    uv run --with mlx-vlm==0.7.12 --with gepa==0.1.4 python eval/ocr_prompt_gepa.py \\
        optimize --bench ../../data/ocr-bench/dataset-v1 --out ../../data/ocr-bench/gepa-v1

Per frame the score is 1 when model and production agree there is no subtitle, the IoU
of the two boxes when both see one, and 0 otherwise (a missed or invented subtitle, or a
reply production could not parse).

The reflection model is Claude Code itself (`claude -p`, the user's subscription, no API
key), as GEPA's own guide does (docs/guides/claude-cli-as-proposer.md). It is allowed to
read files, and the feedback names each frame's image, so it looks at the frames the
prompt got wrong instead of only reading about them.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from common import load_env
from local_ocr_dataset_bench import QWEN_8B, iou, production_box

# One proposal: reading a few frames and writing a prompt.
REFLECTION_TIMEOUT_SECONDS = 900
# Frames per video: half where production saw the subtitle line, half where it did not.
PER_VIDEO = 8


# --- data -----------------------------------------------------------------------------

# Frames production extracted at once when it read every frame at 2 fps; this script
# reproduces that sampling, which production no longer uses.
CHUNK_FRAMES = 32


def target(box, meta):
    """Production's answer as `locate` uses it: a centred box, or None."""
    from localization.activities.detect_subtitles import _centred

    return box if box is not None and _centred(box, meta["width"]) else None


def spread(items: list[int], count: int) -> list[int]:
    if count >= len(items):
        return items
    step = (len(items) - 1) / max(1, count - 1)
    return [items[round(i * step)] for i in range(count)]


def data(args: argparse.Namespace) -> int:
    from functools import partial

    import psycopg
    from common import host_database_url

    from localization.activities.detect_subtitles import SAMPLE_FPS, ocr_size
    from localization.activities.ffmpeg import extract_frames, probe_video
    from localization.config import Settings
    from localization.contracts import SubtitleDocument
    from localization.storage.object_store import ObjectStore

    load_env()
    os.environ["S3_ENDPOINT"] = "http://127.0.0.1:9000"
    settings = Settings()
    store = ObjectStore.from_settings(settings)
    bench = {p.parent.name for p in args.bench.glob("*/video.json")}
    database = host_database_url(settings.loc_database_url.get_secret_value(), args.db_host)
    with psycopg.connect(database) as db:
        rows = db.execute(
            "SELECT aweme_id, generation FROM localization.final_renders "
            "WHERE state = 'rendered' ORDER BY aweme_id"
        ).fetchall()
    rows = [(a, g) for a, g in rows if a not in bench]
    print(f"{len(rows)} approved videos outside the bench's {len(bench)}", flush=True)
    frames_dir = args.out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for aweme, generation in rows:
        prefix = f"work/{aweme}/g{generation}/r0"
        doc = SubtitleDocument.model_validate_json(
            store.get_bytes(f"{prefix}/subtitles.json", produced_by="detect_subtitles")
        )
        if not doc.frame_boxes:
            continue
        meta = {"width": doc.width, "height": doc.height}
        shown = [i for i, b in enumerate(doc.frame_boxes) if target(b, meta)]
        empty = [i for i, b in enumerate(doc.frame_boxes) if not target(b, meta)]
        with_subtitle = spread(shown, PER_VIDEO // 2)
        picked = sorted(with_subtitle + spread(empty, PER_VIDEO - len(with_subtitle)))
        video = args.out / "source.mp4"
        store.download_file(f"{prefix}/source.mp4", video, produced_by="ingest")
        info = probe_video(video)
        scaled = ocr_size(info.width, info.height)
        meta |= {"scaled": list(scaled), "aweme_id": aweme}
        # The chunk production extracted each frame in, so it is the same picture.
        for chunk_start in sorted({i - i % CHUNK_FRAMES for i in picked}):
            tmp = args.out / "chunk"
            tmp.mkdir(exist_ok=True)
            chunk = partial(
                extract_frames, video, tmp, start_s=chunk_start / SAMPLE_FPS,
                count=CHUNK_FRAMES, fps=SAMPLE_FPS, width=scaled[0], height=scaled[1],
                at_end=bool(chunk_start),
            )()  # fmt: skip
            for offset, path in enumerate(chunk):
                index = chunk_start + offset
                if index in picked:
                    name = f"{aweme}-{index:06d}.jpg"
                    path.rename(frames_dir / name)
                    box = target(doc.frame_boxes[index], meta)
                    items.append(
                        meta | {"frame": name, "index": index,
                                "target": box and [box.x, box.y, box.w, box.h]}
                    )  # fmt: skip
                else:
                    path.unlink()
            tmp.rmdir()
        video.unlink()
        print(f"{aweme}: {len(picked)} frames", flush=True)
    # Split by video, so a video's frames are all on one side.
    videos = sorted({i["aweme_id"] for i in items})
    random.Random(0).shuffle(videos)
    val = set(videos[: len(videos) // 2])
    split = {
        "train": [i for i in items if i["aweme_id"] not in val],
        "val": [i for i in items if i["aweme_id"] in val],
    }
    (args.out / "frames.json").write_text(json.dumps(split, indent=1))
    print(f"train {len(split['train'])} frames, val {len(split['val'])} frames")
    return 0


# --- optimize -------------------------------------------------------------------------


def describe(box, meta) -> str:
    """A box in words the reflection model can act on: where it is on the frame."""
    if box is None:
        return "no subtitle"
    w, h = meta["width"], meta["height"]
    cx, cy = (box.x + box.w / 2) / w, (box.y + box.h / 2) / h
    row = "top" if cy < 0.33 else "middle" if cy < 0.66 else "bottom"
    col = "left" if cx < 0.33 else "centre" if cx < 0.66 else "right"
    return (
        f"box at the {row} {col} (x {box.x / w:.2f}-{(box.x + box.w) / w:.2f}, "
        f"y {box.y / h:.2f}-{(box.y + box.h) / h:.2f} of the frame)"
    )


def claude_cli(cwd: Path):
    """GEPA's reflection model: `claude -p`, run where the frames are, allowed to read
    them (and nothing else)."""

    def reflect(prompt) -> str:
        if isinstance(prompt, list):
            prompt = "\n\n".join(m["content"] for m in prompt)
        started = time.time()
        result = subprocess.run(
            ["claude", "-p", "--allowedTools", "Read"],
            input=prompt, capture_output=True, text=True, cwd=cwd,
            timeout=REFLECTION_TIMEOUT_SECONDS,
        )  # fmt: skip
        if result.returncode != 0:
            raise RuntimeError(f"claude -p failed: {result.stderr[-500:]}")
        print(f"  reflection: {time.time() - started:.0f} s", flush=True)
        return result.stdout

    return reflect


def optimize(args: argparse.Namespace) -> int:
    import gepa
    from gepa.core.adapter import EvaluationBatch
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template

    from localization.contracts import BBox

    load_env()
    requests = json.loads((args.bench / "arms.json").read_text())
    production = requests["production"]
    split = json.loads((args.out / "frames.json").read_text())
    frames_dir = args.out / "frames"
    model, processor = load(QWEN_8B, trust_remote_code=False)
    log = (args.out / "evaluations.jsonl").open("a")

    def run(prompt: str, item: dict) -> dict:
        chat = apply_chat_template(processor, model.config, prompt, num_images=1)
        started = time.perf_counter()
        reply = generate(model, processor, chat, [str(frames_dir / item["frame"])],
                         max_tokens=production["max_tokens"], verbose=False)  # fmt: skip
        text = getattr(reply, "text", reply)
        box, unusable = production_box(text, item, production["bbox_format"])
        box = target(box, item)
        want = item["target"] and BBox(**dict(zip("xywh", item["target"], strict=True)))
        if unusable:
            score, feedback = (
                0.0,
                "The reply is not the JSON production parses, so the frame is lost.",
            )
        elif want is None and box is None:
            score, feedback = 1.0, "Correct: no subtitle."
        elif want is None:
            score = 0.0
            feedback = (
                f"Wrong: production saw no burned-in subtitle, but the answer was a "
                f"{describe(box, item)}. That text is something else (a watermark, "
                f"logo, title or on-screen text), not a dialogue subtitle."
            )
        elif box is None:
            score = 0.0
            feedback = (
                f"Missed: the subtitle is a {describe(want, item)}, but the answer "
                f"was no subtitle (or a box off-centre, which is discarded)."
            )
        else:
            score = iou(box, want)
            feedback = (f"Subtitle found, IoU {score:.2f}. Expected {describe(want, item)}; "
                        f"answered {describe(box, item)}.")  # fmt: skip
        result = {"frame": item["frame"], "reply": text, "score": score, "feedback": feedback,
                  "seconds": time.perf_counter() - started}  # fmt: skip
        log.write(json.dumps(result, ensure_ascii=False) + "\n")
        log.flush()
        return result

    class Adapter:
        propose_new_texts = None

        def evaluate(self, batch, candidate, capture_traces=False):
            results = [run(candidate["prompt"], item) for item in batch]
            return EvaluationBatch(
                outputs=[r["reply"] for r in results],
                scores=[r["score"] for r in results],
                trajectories=results if capture_traces else None,
            )

        def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
            return {
                "prompt": [
                    {"Inputs": {"frame": f"frames/{t['frame']} (open it with the Read tool)"},
                     "Generated Outputs": t["reply"],
                     "Feedback": f"score {t['score']:.2f}. {t['feedback']}"}
                    for t in eval_batch.trajectories
                ]
            }  # fmt: skip

    result = gepa.optimize(
        seed_candidate={"prompt": production["prompt"]},
        trainset=split["train"],
        valset=split["val"],
        adapter=Adapter(),
        reflection_lm=claude_cli(args.out),
        reflection_minibatch_size=args.minibatch,
        max_metric_calls=args.max_metric_calls,
        run_dir=str(args.out / "run"),
        seed=0,
    )
    scores = result.val_aggregate_scores
    best = max(range(len(scores)), key=scores.__getitem__)
    prompt = result.candidates[best]["prompt"]
    summary = {
        "reflection_model": "claude -p",
        "metric_calls": result.total_metric_calls,
        "candidates": len(result.candidates),
        "val_score_seed": scores[0],
        "val_score_best": scores[best],
        "prompt": prompt,
    }
    (args.out / "result.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    # The bench's request for its `qwen8b-gepa` arm: production's, with this prompt.
    requests["gepa"] = production | {"prompt": prompt}
    (args.bench / "arms.json").write_text(json.dumps(requests, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="step", required=True)
    for name in ("data", "optimize"):
        p = sub.add_parser(name)
        p.add_argument("--bench", type=Path, required=True, help="the bench's directory")
        p.add_argument("--out", type=Path, required=True, help="this optimisation's directory")
    sub.choices["data"].add_argument("--db-host", default="localhost:5433")
    opt = sub.choices["optimize"]
    opt.add_argument("--max-metric-calls", type=int, default=1500, help="frames asked in all")
    opt.add_argument("--minibatch", type=int, default=8, help="frames per reflection")
    args = parser.parse_args(argv)
    args.bench, args.out = args.bench.resolve(), args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    return {"data": data, "optimize": optimize}[args.step](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
