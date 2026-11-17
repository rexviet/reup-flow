"""Optimises how `detect_subtitles` and `complete_transcript` spend model calls, with GEPA's
`optimize_anything` on code, judged offline on what production already stored.

Today `detect_subtitles` asks the vision model about every frame at 2 fps (about 0.11 USD a
video) for two things: the subtitle region (where the cover goes and the strip that is
read) and the cues (when a subtitle shows), which `complete_transcript` uses to read the
silent stretches where a subtitle shows, to recover lines the ASR missed.

A strategy is Python source defining `plan(video, ask)`. `ask(i)` is the vision model on
frame `i` (2 fps): it answers what production answered on that frame, and each call is
counted. `plan` returns the region and the spans `complete_transcript` reads. The judge:

    lost lines   lines production recovered (lines `complete_transcript` added) that no
                 read span samples any more; `sample_times` and `EDGE_MS` as production
    region       the cover's top and bottom edges against production's, in frame heights
    cost         frames asked x the detect cost a frame + spans read x the read cost a
                 call, both measured from `localization.llm_calls`

    # 1. the dataset (project environment; reads MinIO and Postgres)
    uv run python eval/subtitle_sampling_gepa.py data --bench ../../data/ocr-bench/dataset-v1 \\
        --out ../../data/ocr-bench/sampling-v1
    # 2. today's strategy and the hand-written one, on every split
    uv run python eval/subtitle_sampling_gepa.py baseline --out ../../data/ocr-bench/sampling-v1
    # 3. GEPA, with Claude Code (`claude -p`) as the reflection model
    uv run --with gepa==0.1.4 python eval/subtitle_sampling_gepa.py optimize \\
        --out ../../data/ocr-bench/sampling-v1

The bench's ten videos are the holdout: GEPA never sees them. What this cannot judge is a
line read wrongly where none was before (a new span read over other text): that needs a
real run of the winning strategy before it goes to production.
"""

import argparse
import json
import math
import os
import random
import signal
import statistics
import sys
import time
from pathlib import Path

from common import load_env

# A strategy that loses a recovered line, or moves the cover, pays this much more than
# any saving it could make, so it never wins on cost.
LOST_LINE_USD = 0.01
REGION_TOLERANCE = 0.015  # of the frame height
REGION_MISS_USD = 0.05
PLAN_TIMEOUT_SECONDS = 20

TODAY = '''\
def plan(video, ask):
    """Production today: every frame at 2 fps; the region and cues from `locate`; read
    every silent stretch (>= 1.5 s) during a cue."""
    boxes = [ask(i) for i in range(video.frames)]
    region, cues = video.locate(boxes, step=1)
    spans = video.uncovered_spans(cues) if region else []
    return {"region": region, "read_spans": spans}
'''

HAND = '''\
def plan(video, ask):
    """One frame in eight (every 4 s) for the region; read every silent stretch of
    1.5 s or more, without cues: a stretch with no subtitle reads as empty."""
    step = 8
    boxes = [ask(i) for i in range(0, video.frames, step)]
    region, _ = video.locate(boxes, step=step)
    spans = video.silent_gaps(1500) if region else []
    return {"region": region, "read_spans": spans}
'''

COMBO = '''\
def plan(video, ask):
    """The hand reads (every silent stretch of 1.5 s or more) with GEPA's region search:
    start from a grid of about 8 frames, halve the step until the region agrees with a
    coarser grid's within a third of the 1.5% tolerance on at least 10 boxes. A frame
    asked twice is paid once."""
    n, tol = video.frames, 0.005 * (video.height or 1)
    asked = {}

    def q(i):
        if i not in asked:
            asked[i] = ask(i)
        return asked[i]

    def same(a, b):
        return abs(a[1] - b[1]) <= tol and abs(a[1] + a[3] - b[1] - b[3]) <= tol

    step = 8
    while n // (step * 2) >= 8:
        step *= 2
    region, _ = video.locate([q(i) for i in range(0, n, step)], step=step)
    seen = [region] if region else []
    while step > 8:
        step //= 2
        boxes = [q(i) for i in range(0, n, step)]
        region, _ = video.locate(boxes, step=step)
        hits = sum(1 for b in boxes if b)
        if region and hits >= 10 and any(same(region, r) for r in seen):
            break
        if region:
            seen.append(region)
    spans = video.silent_gaps(1500) if region else []
    return {"region": region, "read_spans": spans}
'''


# --- data -----------------------------------------------------------------------------


def data(args: argparse.Namespace) -> int:
    import psycopg
    from common import host_database_url

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
            "WHERE state = 'rendered' ORDER BY aweme_id, generation"
        ).fetchall()
        costs = dict(
            db.execute(
                "SELECT stage, sum(cost_usd) / count(*) FROM localization.llm_calls "
                "WHERE stage IN ('detect_subtitles', 'complete_transcript') GROUP BY stage"
            ).fetchall()
        )

    def get(key: str) -> bytes:
        return store.get_bytes(key, produced_by="subtitle_sampling_gepa")

    videos = []
    for aweme, generation in rows:
        prefix = f"work/{aweme}/g{generation}/r0/"
        doc = SubtitleDocument.model_validate_json(get(prefix + "subtitles.json"))
        before = json.loads(get(prefix + "transcript.json"))["segments"]
        try:
            after = json.loads(get(prefix + "transcript.subtitles.json"))["segments"]
        except Exception:  # noqa: BLE001 - no line was added
            after = before
        heard = {(s["start_ms"], s["end_ms"]) for s in before}
        videos.append(
            {
                "aweme_id": aweme,
                "generation": generation,
                "bench": aweme in bench,
                "width": doc.width,
                "height": doc.height,
                "boxes": [b and [b.x, b.y, b.w, b.h] for b in doc.frame_boxes],
                "region": doc.region and [doc.region.x, doc.region.y, doc.region.w, doc.region.h],
                "segments": sorted([s["start_ms"], s["end_ms"]] for s in before),
                "recovered": sorted(
                    [s["start_ms"], s["end_ms"]]
                    for s in after
                    if (s["start_ms"], s["end_ms"]) not in heard
                ),
            }
        )
    rest = [v["aweme_id"] for v in videos if not v["bench"]]
    random.Random(0).shuffle(rest)
    val = set(rest[:10])
    for v in videos:
        v["split"] = "holdout" if v["bench"] else "val" if v["aweme_id"] in val else "train"
    meta = {
        "detect_frame_usd": float(costs["detect_subtitles"]),
        "read_call_usd": float(costs["complete_transcript"]),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "dataset.json").write_text(json.dumps({"meta": meta, "videos": videos}))
    for split in ("train", "val", "holdout"):
        chosen = [v for v in videos if v["split"] == split]
        lines = sum(len(v["recovered"]) for v in chosen)
        print(f"{split}: {len(chosen)} videos, {lines} recovered lines")
    print(meta)
    return 0


# --- the judge --------------------------------------------------------------------------


class Video:
    """What a strategy knows about a video before it asks anything, and production's own
    helpers to turn answers into a region, cues and spans."""

    def __init__(self, v: dict) -> None:
        from localization.activities.detect_subtitles import FRAME_INTERVAL_MS

        self.width, self.height = v["width"], v["height"]
        self.frames = len(v["boxes"])
        self.interval_ms = FRAME_INTERVAL_MS
        self.duration_ms = self.frames * FRAME_INTERVAL_MS
        self.segments = [tuple(s) for s in v["segments"]]  # what the ASR heard, in ms

    def locate(self, boxes, step: int = 1):
        """Production's `locate` on boxes asked every `step` frames from frame 0: the
        region (x, y, w, h) or None, and the cues as (start_ms, end_ms)."""
        from localization.activities.detect_subtitles import locate

        region, cues = locate(
            [_box(b) for b in boxes], self.width, self.height, self.duration_ms,
            self.interval_ms * step,
        )  # fmt: skip
        return (region and (region.x, region.y, region.w, region.h)), [
            (c.start_ms, c.end_ms) for c in cues
        ]

    def uncovered_spans(self, cues, min_ms: int = 1500):
        """Production's: the silent stretches (>= `min_ms`) during the cues."""
        from types import SimpleNamespace

        from localization.activities.complete_transcript import uncovered_spans

        cue_objects = [SimpleNamespace(start_ms=a, end_ms=b) for a, b in cues]
        spoken = [SimpleNamespace(start_ms=a, end_ms=b) for a, b in self.segments]
        return uncovered_spans(cue_objects, spoken, min_ms)

    def silent_gaps(self, min_ms: int = 1500):
        """Every silent stretch of `min_ms` or more, over the whole video."""
        return self.uncovered_spans([(0, self.duration_ms)], min_ms)


def _box(raw):
    from localization.contracts import BBox

    return None if raw is None else BBox(x=raw[0], y=raw[1], w=raw[2], h=raw[3])


class _Timeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise _Timeout


def judge(code: str, v: dict, meta: dict) -> tuple[float, dict]:
    """Runs a strategy on one video: its score (minus the cost, in USD, with penalties)
    and what happened, for the reflection model."""
    from localization.activities.complete_transcript import EDGE_MS
    from localization.activities.source_evidence import sample_times

    video, asked = Video(v), set()

    def ask(i: int):
        if not 0 <= i < video.frames:
            raise IndexError(f"frame {i} is outside 0..{video.frames - 1}")
        asked.add(i)
        return v["boxes"][i]

    info: dict = {"video": v["aweme_id"], "frames": video.frames}
    namespace = {"math": math, "statistics": statistics}
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(PLAN_TIMEOUT_SECONDS)
    try:
        exec(compile(code, "<strategy>", "exec"), namespace)  # noqa: S102 - the candidate
        out = namespace["plan"](video, ask)
        region = out.get("region")
        spans = [(int(a), int(b)) for a, b in out.get("read_spans") or []]
    except _Timeout:
        return -1.0, info | {"error": f"plan ran longer than {PLAN_TIMEOUT_SECONDS} s"}
    except Exception as error:  # noqa: BLE001 - a broken candidate scores, never raises
        return -1.0, info | {"error": f"{type(error).__name__}: {error}"}
    finally:
        signal.alarm(0)
    # Reading is production's: frames sampled inside each span, kept off its edges.
    calls, sampled = 0, []
    for a, b in spans:
        a, b = max(0, a), min(video.duration_ms, b)
        if b - a <= 2 * EDGE_MS:
            continue
        calls += 1
        sampled += sample_times(a + EDGE_MS, b - EDGE_MS)
    sampled.sort()
    lost = [
        line for line in v["recovered"]
        if region is None or not any(line[0] <= t <= line[1] for t in sampled)
    ]  # fmt: skip
    reference = v["region"]
    if reference is None or region is None:
        region_error = 0.0 if reference is None and region is None else 1.0
    else:
        top = abs(region[1] - reference[1])
        bottom = abs(region[1] + region[3] - reference[1] - reference[3])
        region_error = max(top, bottom) / video.height
    detect_usd = len(asked) * meta["detect_frame_usd"]
    read_usd = calls * meta["read_call_usd"]
    score = -(detect_usd + read_usd) - LOST_LINE_USD * len(lost)
    if region_error > REGION_TOLERANCE:
        score -= REGION_MISS_USD
    return score, info | {
        "frames_asked": len(asked),
        "spans_read": calls,
        "detect_usd": round(detect_usd, 5),
        "read_usd": round(read_usd, 5),
        "recovered_lines": len(v["recovered"]),
        "lost_lines": len(lost),
        "lost_at_ms": lost[:10],
        "region_error_of_height": round(region_error, 4),
        "region_ok": region_error <= REGION_TOLERANCE,
    }


def summarise(code: str, videos: list[dict], meta: dict) -> dict:
    results = [judge(code, v, meta) for v in videos]
    infos = [i for _, i in results]
    errors = [i["error"] for i in infos if "error" in i]
    ok = [i for i in infos if "error" not in i]
    return {
        "videos": len(videos),
        "errors": errors[:3],
        "usd_per_video": round(sum(i["detect_usd"] + i["read_usd"] for i in ok) / len(ok), 5)
        if ok else None,
        "detect_usd_per_video": round(sum(i["detect_usd"] for i in ok) / len(ok), 5)
        if ok else None,
        "read_usd_per_video": round(sum(i["read_usd"] for i in ok) / len(ok), 5) if ok else None,
        "frames_asked_share": round(sum(i["frames_asked"] for i in ok)
                                    / max(1, sum(i["frames"] for i in ok)), 4),
        "recovered_lines": sum(i["recovered_lines"] for i in ok),
        "lost_lines": sum(i["lost_lines"] for i in ok),
        "videos_region_off": sum(not i["region_ok"] for i in ok),
        "region_error_max": max((i["region_error_of_height"] for i in ok), default=None),
        "score_mean": round(statistics.mean(s for s, _ in results), 5),
        "per_video": infos,
    }  # fmt: skip


def load(out: Path) -> tuple[dict, dict[str, list[dict]]]:
    dataset = json.loads((out / "dataset.json").read_text())
    splits = {s: [v for v in dataset["videos"] if v["split"] == s]
              for s in ("train", "val", "holdout")}  # fmt: skip
    return dataset["meta"], splits


def report(out: Path, strategies: dict[str, str]) -> dict:
    """Every strategy on every split; written to `report.json` for the page."""
    meta, splits = load(out)
    result = {"meta": meta, "strategies": {}}
    for name, code in strategies.items():
        result["strategies"][name] = {
            "code": code,
            **{split: summarise(code, videos, meta) for split, videos in splits.items()},
        }
    (out / "report.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return result


def baseline(args: argparse.Namespace) -> int:
    strategies = {"today": TODAY, "hand": HAND}
    best = args.out / "gepa" / "best_strategy.py"
    if best.exists():
        strategies |= {"gepa": best.read_text(), "combo": COMBO}
    result = report(args.out, strategies)
    for name, s in result["strategies"].items():
        for split in ("train", "val", "holdout"):
            row = {k: v for k, v in s[split].items() if k != "per_video"}
            print(name, split, json.dumps(row))
    return 0


# --- optimize ---------------------------------------------------------------------------

OBJECTIVE = (
    "Write `plan(video, ask)`, the strategy that decides which video frames a vision model "
    "is asked about (each call costs money) and which silent stretches are read for "
    "subtitle text, so that the cost per video is as low as possible while no line "
    "production recovered is lost and the subtitle region stays within 1.5% of the frame "
    "height of production's."
)

BACKGROUND = f"""\
Context: a pipeline dubs short Chinese videos into Vietnamese. Burned-in subtitles are
covered by a box placed at the subtitle region, and the ASR misses some lines, so the
pipeline reads the subtitle strip during silent stretches to recover them.

The judge, per video:
- `ask(i)` returns production's answer for frame i (2 fps, i in 0..video.frames-1): a box
  [x, y, w, h] in source pixels or None. Every distinct frame asked costs
  detect_frame_usd (about $0.00007). Answers are noisy: a few frames carry boxes of
  other text (watermarks, titles); production's `locate` takes a consensus row and drops
  boxes off the frame's centre line.
- The returned "read_spans" are (start_ms, end_ms) stretches. Each span costs one read call
  (about $0.00015) and is sampled like production (`sample_times`: up to 8 frames, about
  one per 700 ms, kept {300} ms off each edge). A recovered line survives only if some
  sampled time falls inside it. Spans over speech are allowed but useless.
- The returned "region" is (x, y, w, h) or None. Only its top and bottom edges matter (the
  cover's width comes from the Vietnamese caption); they must be within 1.5% of the
  frame height of production's region. No region means nothing is read at all.
- Score = -(detect + read cost) - {LOST_LINE_USD} per lost line - {REGION_MISS_USD} if the
  region is off. Higher is better.

Available in the strategy: `math`, `statistics`, and on `video`: `frames`, `width`,
`height`, `interval_ms` (500), `duration_ms`, `segments` (the ASR's spoken
(start_ms, end_ms), known before asking anything), `locate(boxes, step)` (production's
region and cues from boxes asked every `step` frames starting at frame 0),
`uncovered_spans(cues, min_ms=1500)` (silent stretches during cues) and
`silent_gaps(min_ms=1500)` (all silent stretches). Keep `plan` deterministic and fast.
"""


def optimize(args: argparse.Namespace) -> int:
    from gepa.optimize_anything import (
        EngineConfig,
        GEPAConfig,
        ReflectionConfig,
        optimize_anything,
    )
    from ocr_prompt_gepa import claude_cli

    meta, splits = load(args.out)

    def evaluate(candidate: str, example: dict) -> tuple[float, dict]:
        return judge(candidate, example, meta)

    work = args.out / "gepa"
    work.mkdir(exist_ok=True)
    result = optimize_anything(
        seed_candidate=HAND,
        evaluator=evaluate,
        dataset=splits["train"],
        valset=splits["val"],
        objective=OBJECTIVE,
        background=BACKGROUND,
        config=GEPAConfig(
            engine=EngineConfig(
                run_dir=str(work / "run"),
                max_metric_calls=args.max_metric_calls,
                max_candidate_proposals=args.proposals,
                parallel=False,  # the judge times out a strategy with SIGALRM
            ),
            reflection=ReflectionConfig(
                reflection_lm=claude_cli(work),
                reflection_minibatch_size=args.minibatch,
            ),
        ),
    )
    best = result.best_candidate
    code = best if isinstance(best, str) else next(iter(best.values()))
    (work / "best_strategy.py").write_text(code)
    final = report(args.out, {"today": TODAY, "hand": HAND, "gepa": code})
    for name, s in final["strategies"].items():
        row = {k: v for k, v in s["holdout"].items() if k != "per_video"}
        print(name, "holdout", json.dumps(row))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="step", required=True)
    for name in ("data", "baseline", "optimize"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=Path, required=True, help="this study's directory")
    sub.choices["data"].add_argument("--bench", type=Path, required=True)
    sub.choices["data"].add_argument("--db-host", default="localhost:5433")
    opt = sub.choices["optimize"]
    opt.add_argument("--max-metric-calls", type=int, default=3000, help="video judgements")
    opt.add_argument("--proposals", type=int, default=30, help="strategies Claude writes")
    opt.add_argument("--minibatch", type=int, default=6, help="videos per reflection")
    args = parser.parse_args(argv)
    args.out = args.out.resolve()
    if getattr(args, "bench", None):
        args.bench = args.bench.resolve()
    started = time.time()
    code = {"data": data, "baseline": baseline, "optimize": optimize}[args.step](args)
    print(f"done in {time.time() - started:.0f} s")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
