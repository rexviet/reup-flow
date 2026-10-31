"""Can the OCR model read on-screen text and tell an overlay from text in the scene?

    uv run python eval/overlay_text_spike.py --video path.mp4 [--every 1.0] [--out-dir dir]

Each sampled frame goes to the configured OCR model with a prompt that asks for every piece
of text, its box and its kind: `overlay` (added in editing: titles, handwritten captions,
stickers), `scene` (printed on products, signs, clothes) or `subtitle` (the spoken words at the
bottom). The script prints how many items of each kind it found, the distinct overlay texts
with the first and last second each was seen, and writes frames with the boxes drawn on them,
so the reading can be checked by eye against the video.
"""

import argparse
import asyncio
import base64
import collections
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from common import load_env
from PIL import Image, ImageDraw

from localization.config import Settings

PROMPT = (
    "List every piece of text visible in this video frame. Reply with JSON only: "
    '{"items": [{"text": "...", "bbox": [x1, y1, x2, y2], "kind": "overlay|scene|subtitle"}]}. '
    "bbox is the tightest box around that text, in 0-1000 units of the image width and height. "
    "kind: overlay = text added in video editing (a title, a handwritten caption, a sticker, a "
    "label drawn on top of the picture); scene = text that is part of what was filmed (printed on "
    "a product, packaging, a sign, clothing); subtitle = the lines at the bottom that transcribe "
    "what is being said. Copy the text exactly as written. Reply with an empty list when the frame "
    "has no text."
)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def frames(video: Path, every: float, out_dir: Path) -> list[tuple[float, Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", f"fps=1/{every},scale=720:-1",
         "-q:v", "3", str(out_dir / "f%04d.jpg")],
        check=True,
    )
    files = sorted(out_dir.glob("f*.jpg"))
    return [(i * every, p) for i, p in enumerate(files)]


async def ask(client: httpx.AsyncClient, base: str, key: str, model: str, frame: Path) -> list[dict] | str:
    image = base64.b64encode(frame.read_bytes()).decode("ascii")
    body = {
        "model": model,
        "reasoning": {"enabled": False},
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
        ]}],
    }
    for attempt in range(3):
        try:
            r = await client.post(f"{base}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}, timeout=120)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return json.loads(_FENCE.sub("", content.strip()))["items"]
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            if attempt == 2:
                return f"{type(exc).__name__}: {str(exc)[:80]}"
            await asyncio.sleep(2)
    return "unreachable"


def draw(frame: Path, items: list[dict], dest: Path) -> None:
    image = Image.open(frame).convert("RGB")
    w, h = image.size
    canvas = ImageDraw.Draw(image)
    colors = {"overlay": "#ff2d2d", "scene": "#2d6bff", "subtitle": "#18a34a"}
    for it in items:
        try:
            x1, y1, x2, y2 = it["bbox"]
            canvas.rectangle([x1 * w / 1000, y1 * h / 1000, x2 * w / 1000, y2 * h / 1000],
                             outline=colors.get(it.get("kind"), "#ffaa00"), width=3)
        except (KeyError, TypeError, ValueError):
            continue
    image.save(dest)


async def main_async(args: argparse.Namespace) -> int:
    load_env()
    settings = Settings()
    key = settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else None
    if not key or not settings.ocr_model:
        print("OPENROUTER_API_KEY and OCR_MODEL must be set")
        return 2
    out_dir = Path(args.out_dir)
    sampled = frames(Path(args.video), args.every, out_dir / "frames")
    print(f"{len(sampled)} frame(s) every {args.every}s, model {settings.ocr_model}")
    gate = asyncio.Semaphore(args.concurrency)
    results: dict[float, list[dict] | str] = {}
    began = time.perf_counter()
    async with httpx.AsyncClient() as client:
        async def one(t: float, path: Path) -> None:
            async with gate:
                results[t] = await ask(client, settings.openrouter_base_url.rstrip("/"), key, settings.ocr_model, path)
        await asyncio.gather(*(one(t, p) for t, p in sampled))
    print(f"asked in {time.perf_counter() - began:.0f}s")
    failed = {t: r for t, r in results.items() if isinstance(r, str)}
    print("frames with an unusable answer:", len(failed), list(failed.values())[:3])
    kinds = collections.Counter(it.get("kind") for r in results.values() if isinstance(r, list) for it in r)
    print("items by kind:", dict(kinds))
    seen: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for t, r in sorted(results.items()):
        if isinstance(r, list):
            for it in r:
                seen[(it.get("kind", "?"), str(it.get("text", "")).strip())].append(t)
    for kind in ("overlay", "scene", "subtitle"):
        rows = [(txt, ts) for (k, txt), ts in seen.items() if k == kind]
        print(f"\n== {kind}: {len(rows)} distinct text(s)")
        for txt, ts in sorted(rows, key=lambda r: r[1][0])[: args.show]:
            print(f"  {ts[0]:5.0f}s-{ts[-1]:5.0f}s  x{len(ts):<3d} {txt[:60]}")
    marks = out_dir / "marked"
    marks.mkdir(exist_ok=True)
    by_time = dict(sampled)
    for t in args.mark:
        near = min(by_time, key=lambda s: abs(s - t))
        if isinstance(results.get(near), list):
            draw(by_time[near], results[near], marks / f"t{int(near):03d}.png")
    (out_dir / "results.json").write_text(json.dumps({str(t): r for t, r in sorted(results.items())}, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--video", required=True)
    parser.add_argument("--every", type=float, default=1.0)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--show", type=int, default=40)
    parser.add_argument("--mark", type=float, nargs="*", default=[30, 50, 55])
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
