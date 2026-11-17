"""Spike: watermarks found from images a collection supplies, then blurred.

A collection has zero to a few watermark images. With none, nothing is done. With some,
every frame at 2 fps is searched for each image by template matching on edges (Canny):
an edge picture ignores the background behind semi-transparent text, which plain
grey matching does not. Every place a template matches above `THRESHOLD` is a hit (more
than one per frame: the same watermark in two corners). Hits are blurred as the render
blurs (`activities.render`: a box blur twice, over the box padded by `PAD` of its height).

    uv run --with opencv-python-headless python spikes/watermark_template_spike.py \\
        --out ../../data/ocr-bench/watermark-templates --s3-endpoint http://127.0.0.1:9000

Writes `<out>/index.html`: per case, the counts and a before/after of chosen frames.
Templates are cut from one frame of one video (standing in for the image a person would
upload); each is searched on every video of both collections, so the other collection's
videos show what the template finds where its watermark is absent.
"""

import argparse
import html
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

THRESHOLD = 0.7
# Peaks kept per template per frame: a watermark in two corners at once is two.
MAX_HITS = 4
PAD = 0.3
MAX_BLUR_RADIUS = 20
TEMPLATE_HEIGHT = 1080  # the height of the frames the templates were cut from
# One collection's videos show its watermark at different sizes (a smaller handle in some
# sources, a larger disclaimer in others), and a video can change it midway.
SCALES = [round(0.6 + 0.05 * i, 2) for i in range(19)]  # 0.6 .. 1.5
CALIBRATION_FRAMES = 24
MAX_SCALES = 3
# A hit must hold at least this share of the template's edge pixels: on a dark or flat
# patch the correlation is taken over noise and can score high on nothing.
MIN_INK = 0.5
# A hit mostly inside the burned-in subtitle band is subtitle text, not the watermark.
INSIDE_SUBTITLE = 0.5
# A watermark stays put: a place hit on fewer frames in a row than this (gaps of up to
# `MAX_GAP` frames bridged) is a stray match. 4 frames at 2 fps is 2 s.
MIN_RUN = 4
MAX_GAP = 2


@dataclass
class Template:
    name: str
    collection: str
    video: str
    at_s: float
    box: tuple[int, int, int, int]  # x, y, w, h in a 1080p frame


TEMPLATES = [
    Template("handle", "Hoán Đổi Thân Xác Với Một Cô Gái Nhật Bản", "7677937301649142961", 60,
             (14, 110, 272, 62)),
    Template("disclaimer", "Cái Giá Của Danh Vọng", "7704493353806728448", 163,
             (30, 4, 440, 70)),
]  # fmt: skip


@dataclass
class Case:
    name: str
    video: Path
    collection: str
    note: str
    hits: list[list[tuple[str, int, int, int, int, float]]] = field(default_factory=list)
    best: list[dict[str, float]] = field(default_factory=list)
    width: int = 0
    height: int = 0
    match_ms: float = 0.0
    calibrate_s: float = 0.0
    scales: dict[str, list[float]] = field(default_factory=dict)
    subtitle: tuple[int, int, int, int] | None = None
    raw_hits: int = 0
    dropped: dict[str, int] = field(default_factory=lambda: {"ink": 0, "subtitle": 0, "run": 0})


def run(cmd: list[str]) -> bytes:
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def probe(video: Path) -> tuple[int, int]:
    out = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "json", str(video)]))  # fmt: skip
    s = out["streams"][0]
    return s["width"], s["height"]


def grab(video: Path, t: float, gray: bool = False) -> np.ndarray:
    w, h = probe(video)
    raw = run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray" if gray else "bgr24",
            "-",
        ]
    )
    return np.frombuffer(raw, np.uint8).reshape((h, w) if gray else (h, w, 3))  # fmt: skip


def edges(image: np.ndarray) -> np.ndarray:
    """Edges, softened: a watermark shown a little larger or smaller than its template
    still lands on the template's edges, which hard one-pixel edges do not."""
    return cv2.GaussianBlur(cv2.Canny(image, 80, 200), (7, 7), 0)


def peaks(result: np.ndarray, tw: int, th: int) -> list[tuple[int, int, float]]:
    found = []
    result = result.copy()
    for _ in range(MAX_HITS):
        _, best, _, (x, y) = cv2.minMaxLoc(result)
        if best < THRESHOLD:
            break
        found.append((x, y, float(best)))
        # Suppress the neighbourhood: one watermark is one hit.
        result[max(0, y - th) : y + th, max(0, x - tw) : x + tw] = -1
    return found


def frames_2fps(video: Path, width: int, height: int):
    """Every frame at 2 fps, grey, at the source size."""
    decode = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vf", "fps=2",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE,
    )  # fmt: skip
    size = width * height
    while (chunk := decode.stdout.read(size)) and len(chunk) == size:
        yield np.frombuffer(chunk, np.uint8).reshape(height, width)
    decode.wait()


def sized(template: np.ndarray, scale: float) -> np.ndarray:
    resized = cv2.resize(
        template,
        None,
        fx=scale,
        fy=scale,
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
    )
    return edges(resized)


def calibrate(case: Case, templates: dict[str, np.ndarray]) -> dict[str, list[float]]:
    """The sizes each template shows at in this video: every `SCALES` factor (of the
    video's height over the template's) tried on `CALIBRATION_FRAMES` frames spread over
    the video; the best `MAX_SCALES` factors that reach the threshold are kept."""
    base = case.height / TEMPLATE_HEIGHT
    duration = float(json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                                     "format=duration", "-of", "json", str(case.video)]))
                     ["format"]["duration"])  # fmt: skip
    every = max(1, int(duration * 2) // CALIBRATION_FRAMES)
    best: dict[str, dict[float, float]] = {n: {} for n in templates}
    variants = {n: {f: sized(t, base * f) for f in SCALES} for n, t in templates.items()}
    for i, frame in enumerate(frames_2fps(case.video, case.width, case.height)):
        if i % every:
            continue
        picture = edges(frame)
        for name, by_factor in variants.items():
            for factor, template in by_factor.items():
                if template.shape[0] >= picture.shape[0] or template.shape[1] >= picture.shape[1]:
                    continue
                score = float(cv2.matchTemplate(picture, template, cv2.TM_CCOEFF_NORMED).max())
                best[name][factor] = max(best[name].get(factor, 0.0), score)
    kept = {}
    for name, scores in best.items():
        good = sorted((s, f) for f, s in scores.items() if s >= THRESHOLD)[::-1]
        kept[name] = [base * f for _, f in good[:MAX_SCALES]]
    return kept


def _inside(hit, region) -> float:
    _, x, y, w, h, _ = hit
    rx, ry, rw, rh = region
    ix = max(0, min(x + w, rx + rw) - max(x, rx))
    iy = max(0, min(y + h, ry + rh) - max(y, ry))
    return ix * iy / (w * h)


def search(case: Case, templates: dict[str, np.ndarray]) -> None:
    case.width, case.height = probe(case.video)
    started = time.perf_counter()
    case.scales = calibrate(case, templates)
    case.calibrate_s = time.perf_counter() - started
    variants = []
    for name, scales in case.scales.items():
        for scale in scales:
            resized = cv2.resize(
                templates[name],
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
            )
            ink = int(np.count_nonzero(cv2.Canny(resized, 80, 200)))
            variants.append((name, edges(resized), ink))  # fmt: skip
    spent = 0.0
    for frame in frames_2fps(case.video, case.width, case.height):
        started = time.perf_counter()
        hard = cv2.Canny(frame, 80, 200)
        picture = cv2.GaussianBlur(hard, (7, 7), 0)
        hits, best = [], {n: 0.0 for n in templates}
        for name, template, ink in variants:
            th, tw = template.shape
            result = cv2.matchTemplate(picture, template, cv2.TM_CCOEFF_NORMED)
            best[name] = max(best[name], float(result.max()))
            for x, y, score in peaks(result, tw, th):
                case.raw_hits += 1
                if np.count_nonzero(hard[y : y + th, x : x + tw]) < MIN_INK * ink:
                    case.dropped["ink"] += 1
                    continue
                hit = (name, x, y, tw, th, score)
                if case.subtitle and _inside(hit, case.subtitle) >= INSIDE_SUBTITLE:
                    case.dropped["subtitle"] += 1
                    continue
                hits.append(hit)
        # The same watermark found at two sizes is one hit: keep the better.
        hits.sort(key=lambda h: -h[5])
        kept = []
        for h in hits:
            if all(abs(h[1] - k[1]) > k[3] // 2 or abs(h[2] - k[2]) > k[4] // 2 for k in kept):
                kept.append(h)
        spent += time.perf_counter() - started
        case.hits.append(kept)
        case.best.append(best)
    case.match_ms = 1000 * spent / max(1, len(case.hits))
    persist(case)


def persist(case: Case) -> None:
    """Drops hits at a place not held for `MIN_RUN` frames in a row."""
    places: list[tuple[str, int, int, int, int, list[int]]] = []  # name, x, y, w, h, frames
    for i, hits in enumerate(case.hits):
        for name, x, y, w, h, _ in hits:
            for p in places:
                if p[0] == name and abs(p[1] - x) <= w // 4 and abs(p[2] - y) <= h // 2:
                    p[5].append(i)
                    break
            else:
                places.append((name, x, y, w, h, [i]))
    keep: set[tuple[int, str, int, int]] = set()
    for name, px, py, w, h, frames in places:
        runs, run = [], [frames[0]]
        for i in frames[1:]:
            if i - run[-1] - 1 <= MAX_GAP:
                run.append(i)
            else:
                runs.append(run)
                run = [i]
        runs.append(run)
        for r in runs:
            if r[-1] - r[0] + 1 >= MIN_RUN:
                keep.update((i, name, px, py) for i in r)
    for i, hits in enumerate(case.hits):
        kept = []
        for hit in hits:
            name, x, y, w, h, _ = hit
            if any(
                (i, name, px, py) in keep and abs(px - x) <= w // 4 and abs(py - y) <= h // 2
                for _, n2, px, py in [(i, name, p[1], p[2]) for p in places if p[0] == name]
            ):
                kept.append(hit)
            else:
                case.dropped["run"] += 1
        case.hits[i] = kept


def blur(image: np.ndarray, hits) -> np.ndarray:
    out = image.copy()
    h_img, w_img = image.shape[:2]
    for _, x, y, w, h, _ in hits:
        pad = int(np.ceil(PAD * h))
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(w_img, x + w + pad), min(h_img, y + h + pad)
        radius = max(1, min(MAX_BLUR_RADIUS, min(x2 - x1, y2 - y1) // 4 - 1))
        region = out[y1:y2, x1:x2]
        for _ in range(2):
            region = cv2.blur(region, (2 * radius + 1, 2 * radius + 1))
        out[y1:y2, x1:x2] = region
    return out


def chosen_frames(case: Case) -> list[int]:
    """First and middle hit, the frame before the first hit, and a frame without one."""
    hit = [i for i, h in enumerate(case.hits) if h]
    miss = [i for i, h in enumerate(case.hits) if not h]
    picks = []
    if hit:
        picks += [hit[0], hit[len(hit) // 2], hit[-1]]
        if hit[0] > 0:
            picks.append(hit[0] - 1)
    if miss:
        picks.append(miss[len(miss) // 2])
    return sorted(set(picks))[:5]


def render_frames(case: Case, out: Path) -> list[tuple[int, str, str]]:
    shots = []
    for i in chosen_frames(case):
        image = grab(case.video, i / 2)
        before = image.copy()
        for _, x, y, w, h, s in case.hits[i]:
            cv2.rectangle(before, (x, y), (x + w, y + h), (0, 255, 0), 3)
            cv2.putText(before, f"{s:.2f}", (x, y + h + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                        (0, 255, 0), 2)  # fmt: skip
        after = blur(image, case.hits[i])
        pair = np.hstack([before, after])
        pair = cv2.resize(pair, (1600, round(1600 * pair.shape[0] / pair.shape[1])))
        name = f"{case.name}-{i}.jpg"
        cv2.imwrite(str(out / "frames" / name), pair, [cv2.IMWRITE_JPEG_QUALITY, 82])
        label = ", ".join(f"{n} {s:.2f}" for n, *_, s in case.hits[i]) or "no hit"
        shots.append((i, name, label))
    return shots


def download(aweme: str, out: Path, endpoint: str | None) -> Path | None:
    target = out / "videos" / f"{aweme}.mp4"
    if target.exists():
        return target
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
    from common import load_env

    from localization.config import Settings
    from localization.storage.object_store import ObjectStore

    load_env()
    if endpoint:
        os.environ["S3_ENDPOINT"] = endpoint
    store = ObjectStore.from_settings(Settings())
    listing = store._client.list_objects_v2(Bucket=store.bucket, Prefix=f"work/{aweme}/")
    keys = sorted(
        o["Key"] for o in listing.get("Contents", []) if o["Key"].endswith("/r0/source.mp4")
    )
    if not keys:
        return None
    store.download_file(keys[-1], target, produced_by="ingest")
    return target


def subtitle_region(aweme: str, out: Path, endpoint: str | None):
    """The burned-in subtitle band production found on this video (x, y, w, h), or None."""
    cached = out / "subtitles" / f"{aweme}.json"
    if not cached.exists():
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
        from common import load_env

        from localization.config import Settings
        from localization.storage.object_store import ObjectStore

        load_env()
        if endpoint:
            os.environ["S3_ENDPOINT"] = endpoint
        store = ObjectStore.from_settings(Settings())
        listing = store._client.list_objects_v2(Bucket=store.bucket, Prefix=f"work/{aweme}/")
        keys = sorted(
            o["Key"] for o in listing.get("Contents", []) if o["Key"].endswith("/r0/subtitles.json")
        )
        cached.write_bytes(
            store.get_bytes(keys[-1], produced_by="detect_subtitles") if keys else b"{}"
        )
    region = json.loads(cached.read_text()).get("region")
    return None if region is None else (region["x"], region["y"], region["w"], region["h"])


def synthetic(base: Path, out: Path) -> list[tuple[str, Path, str]]:
    """Cases made from the handle video: the watermark moving from the left corner to the
    right, in both corners at once, and the video at 720p."""
    x, y, w, h = TEMPLATES[0].box
    cases = []
    moved = out / "videos" / "synthetic-moves-left-to-right.mp4"
    if not moved.exists():
        # 0-30 s as it is; 30-60 s mirrored, with the live watermark patch laid over the
        # mirrored one, so it reads correctly in the right corner and the left is empty.
        graph = (
            f"[0:v]split[a][b];[b]crop={w + 20}:{h + 20}:{x - 10}:{y - 10}[p];"
            f"[a]hflip[f];[f][p]overlay=x=W-{x + w + 10}:y={y - 10}[m]"
        )
        _ffmpeg_two_halves(base, moved, graph)
    cases.append(("moves", moved, "0–30 s watermark at the left; 30–60 s at the right"))
    both = out / "videos" / "synthetic-both-corners.mp4"
    if not both.exists():
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "40", "-t", "30", "-i", str(base),
             "-filter_complex",
             f"[0:v]split[a][b];[b]crop={w + 20}:{h + 20}:{x - 10}:{y - 10}[p];"
             f"[a][p]overlay=x=W-{x + w + 10}:y={y - 10}",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(both)],
            check=True,
        )  # fmt: skip
    cases.append(("both", both, "the watermark in both top corners at once"))
    small = out / "videos" / "synthetic-720p.mp4"
    if not small.exists():
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "40", "-t", "30", "-i", str(base),
             "-vf", "scale=1280:720", "-an", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", str(small)],
            check=True,
        )  # fmt: skip
    cases.append(("720p", small, "the 1080p template on the video scaled to 720p"))
    return cases


def _ffmpeg_two_halves(base: Path, out: Path, mirrored_graph: str) -> None:
    first, second = out.with_suffix(".a.mp4"), out.with_suffix(".b.mp4")
    enc = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "40", "-t", "30", "-i",
                    str(base), *enc, str(first)], check=True)  # fmt: skip
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "70", "-t", "30", "-i",
                    str(base), "-filter_complex", mirrored_graph, "-map", "[m]", *enc,
                    str(second)], check=True)  # fmt: skip
    listing = out.with_suffix(".txt")
    listing.write_text(f"file '{first.name}'\nfile '{second.name}'\n")
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c", "copy", str(out)], check=True)  # fmt: skip
    for p in (first, second, listing):
        p.unlink()


def page(cases: list[Case], shots: dict[str, list], templates: list[Template]) -> str:
    rows = []
    for c in cases:
        n = len(c.hits)
        per = {t.name: sum(any(h[0] == t.name for h in hits) for hits in c.hits) for t in templates}
        places = {t.name: len({(h[1] // 8, h[2] // 8) for hits in c.hits for h in hits
                              if h[0] == t.name}) for t in templates}  # fmt: skip
        miss_best = {
            t.name: max((b[t.name] for b, hits in zip(c.best, c.hits, strict=True)
                         if not any(h[0] == t.name for h in hits)), default=None)
            for t in templates
        }  # fmt: skip
        cells = "".join(
            f"<td>{per[t.name]}/{n}<br><small>{places[t.name]} place(s); best without: "
            f"{'–' if miss_best[t.name] is None else f'{miss_best[t.name]:.2f}'}</small></td>"
            for t in templates
        )
        rows.append(
            f"<tr><td><a href='#{c.name}'>{html.escape(c.name)}</a><br><small>"
            f"{html.escape(c.collection)} · {c.width}×{c.height}</small></td>{cells}"
            f"<td>{c.match_ms:.0f}<br><small>calibrate {c.calibrate_s:.0f} s; sizes "
            f"{html.escape(json.dumps({k: [round(x, 2) for x in v] for k, v in c.scales.items()}))}"
            f"</small></td><td>{html.escape(c.note)}<br><small>raw matches {c.raw_hits}; "
            f"dropped {html.escape(json.dumps(c.dropped))}</small></td></tr>"
        )
    heads = "".join(f"<th>{html.escape(t.name)} frames hit</th>" for t in templates)
    sections = []
    for c in cases:
        figs = (
            "".join(
                f"<figure><img src='frames/{n}' loading='lazy'><figcaption>{i / 2:.1f} s · "
                f"{html.escape(label)}</figcaption></figure>"
                for i, n, label in shots[c.name]
            )
            or "<p>No frame to show.</p>"
        )
        sections.append(
            f"<section id='{c.name}'><h2>{html.escape(c.name)}</h2><p>{html.escape(c.note)}"
            f"</p>{figs}</section>"
        )
    tmpl = "".join(
        f"<figure class='t'><img src='templates/{t.name}.png'><figcaption>{t.name}: "
        f"{html.escape(t.collection)}</figcaption></figure>"
        for t in templates
    )
    return f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watermark templates</title><style>
body{{font:14px/1.5 system-ui,sans-serif;margin:24px;background:#111;color:#eee}}
table{{border-collapse:collapse;margin:12px 0}}td,th{{border:1px solid #444;padding:6px 10px;
text-align:left;vertical-align:top}}small{{color:#aaa}}a{{color:#8cf}}
figure{{margin:12px 0}}img{{max-width:100%;border:1px solid #333}}figure.t{{display:inline-block;
margin-right:24px}}figure.t img{{background:#fff;max-height:80px}}section{{margin-top:32px}}
</style></head><body>
<h1>Watermark templates: detect and blur</h1>
<p>Each frame: left the source with the hits (green box, match score), right the blur the
render would apply. Threshold {THRESHOLD}, 2 frames a second, edge matching.</p>
<h3>Templates</h3>{tmpl}
<table><tr><th>Case</th>{heads}<th>ms / frame</th><th>Note</th></tr>{"".join(rows)}</table>
{"".join(sections)}</body></html>"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--collections", type=Path, required=True,
                        help="lines 'collection name|aweme id'")  # fmt: skip
    parser.add_argument("--s3-endpoint", default=None)
    args = parser.parse_args(argv)
    for sub in ("videos", "frames", "templates", "subtitles"):
        (args.out / sub).mkdir(parents=True, exist_ok=True)
    templates = {}
    for t in TEMPLATES:
        video = download(t.video, args.out, args.s3_endpoint)
        x, y, w, h = t.box
        templates[t.name] = grab(video, t.at_s, gray=True)[y : y + h, x : x + w].copy()
        cv2.imwrite(str(args.out / "templates" / f"{t.name}.png"), templates[t.name])
    cases = []
    for line in args.collections.read_text().splitlines():
        collection, aweme = line.split("|")
        video = download(aweme, args.out, args.s3_endpoint)
        if video is None:
            print(f"{aweme}: no source kept, skipped", flush=True)
            continue
        case = Case(aweme, video, collection, "real video")
        case.subtitle = subtitle_region(aweme, args.out, args.s3_endpoint)
        cases.append(case)
    handle_video = args.out / "videos" / f"{TEMPLATES[0].video}.mp4"
    base_band = subtitle_region(TEMPLATES[0].video, args.out, args.s3_endpoint)
    for name, path, note in synthetic(handle_video, args.out):
        case = Case(f"synthetic-{name}", path, TEMPLATES[0].collection, note)
        if base_band:  # the subtitle is centred, so the mirrored half keeps it in place
            k = 720 / 1080 if name == "720p" else 1.0
            case.subtitle = tuple(round(v * k) for v in base_band)
        cases.append(case)
    shots = {}
    for case in cases:
        search(case, templates)
        shots[case.name] = render_frames(case, args.out)
        hit = sum(bool(h) for h in case.hits)
        print(f"{case.name}: {hit}/{len(case.hits)} frames hit, {case.match_ms:.0f} ms/frame, "
              f"raw {case.raw_hits}, dropped {case.dropped}", flush=True)  # fmt: skip
    (args.out / "index.html").write_text(page(cases, shots, TEMPLATES))
    summary = {
        c.name: {
            "collection": c.collection,
            "frames": len(c.hits),
            "hit_frames": {
                t.name: sum(any(h[0] == t.name for h in hits) for hits in c.hits) for t in TEMPLATES
            },
            "two_hits_frames": sum(len(h) > 1 for h in c.hits),
            "scores_when_hit": {
                t.name: statistics.median(
                    [h[5] for hits in c.hits for h in hits if h[0] == t.name] or [0]
                )
                for t in TEMPLATES
            },
            "best_when_missed": {
                t.name: max(
                    (
                        b[t.name]
                        for b, hits in zip(c.best, c.hits, strict=True)
                        if not any(h[0] == t.name for h in hits)
                    ),
                    default=None,
                )
                for t in TEMPLATES
            },  # fmt: skip
            "match_ms": c.match_ms,
            "calibrate_s": c.calibrate_s,
            "scales": c.scales,
            "raw_hits": c.raw_hits,
            "dropped": c.dropped,
        }
        for c in cases
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
