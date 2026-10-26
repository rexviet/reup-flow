"""G5: can a vision LLM return the subtitle strip's bbox well enough?
Pass = median IoU >= 0.7 AND p10 IoU >= 0.5 over the labelled frames.

Run:  ./run.sh uv run python g5_ocr_bbox.py            # needs OPENROUTER_API_KEY, OCR_MODEL
      ./run.sh uv run python g5_ocr_bbox.py --selftest

Labels live in fixtures/g5-labels.json: [{"image": "frames/x.png", "bbox": [x0,y0,x1,y1]}]
in PIXELS of that image. They must be drawn by a human on frames that really
have a burned-in subtitle; the shipped file is an empty template on purpose.
"""

from __future__ import annotations

import base64
import json
import os
import re
import statistics
import sys
from pathlib import Path

import httpx
from PIL import Image

BBOX_FORMATS = {
    # name -> converter from the model's 4 numbers to pixel [x0,y0,x1,y1]
    "xyxy_px": lambda b, w, h: b,
    "xyxy_norm1": lambda b, w, h: [b[0] * w, b[1] * h, b[2] * w, b[3] * h],
    "xyxy_norm1000": lambda b, w, h: [b[0] * w / 1000, b[1] * h / 1000, b[2] * w / 1000, b[3] * h / 1000],
    "yxyx_norm1000": lambda b, w, h: [b[1] * w / 1000, b[0] * h / 1000, b[3] * w / 1000, b[2] * h / 1000],
}


def iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def p10(xs: list[float]) -> float:
    s = sorted(xs)
    return s[max(0, int(0.1 * len(s)) - (0 if len(s) % 10 else 0))] if s else 0.0


def selftest() -> int:
    assert iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert abs(iou([0, 0, 10, 10], [5, 0, 15, 10]) - 1 / 3) < 1e-9
    assert BBOX_FORMATS["yxyx_norm1000"](list(map(float, [500, 0, 600, 1000])), 100, 200) == [0, 100, 100, 120]
    print("selftest ok")
    return 0


def ask(model: str, image: Path, base: str, key: str) -> list[float]:
    b64 = base64.b64encode(image.read_bytes()).decode()
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        timeout=120,
        json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Return ONLY the bounding box of the burned-in subtitle text strip as JSON [a,b,c,d]."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
        },
    )
    r.raise_for_status()
    nums = re.findall(r"-?\d+(?:\.\d+)?", r.json()["choices"][0]["message"]["content"])[:4]
    return [float(n) for n in nums]


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    key, model = os.environ.get("OPENROUTER_API_KEY"), os.environ.get("OCR_MODEL")
    fmt = os.environ.get("OCR_BBOX_FORMAT", "")
    labels = json.loads(Path("fixtures/g5-labels.json").read_text())
    reasons = []
    if not key or not model:
        reasons.append("OPENROUTER_API_KEY / OCR_MODEL not set")
    if fmt not in BBOX_FORMATS:
        reasons.append(f"OCR_BBOX_FORMAT must be one of {sorted(BBOX_FORMATS)}")
    if len(labels) < 20:
        reasons.append(f"fixtures/g5-labels.json has {len(labels)} hand-labelled frames, need 20 from 5 videos with burned-in subtitles")
    if reasons:
        print(json.dumps({"verdict": "BLOCKED", "reasons": reasons}, indent=2))
        return 2
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    scores = []
    for item in labels:
        img = Path(item["image"])
        w, h = Image.open(img).size
        scores.append(iou(BBOX_FORMATS[fmt](ask(model, img, base, key), w, h), item["bbox"]))
    med, low = statistics.median(scores), p10(scores)
    verdict = "PASS" if med >= 0.7 and low >= 0.5 else "FAIL"
    out = {"model": model, "format": fmt, "n": len(scores), "median_iou": round(med, 3), "p10_iou": round(low, 3), "verdict": verdict,
           "if_fail": "write a second OCR implementation (dedicated OCR)", "reproduce": "./run.sh uv run python g5_ocr_bbox.py"}
    Path("out").mkdir(exist_ok=True)
    Path("out/g5.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
