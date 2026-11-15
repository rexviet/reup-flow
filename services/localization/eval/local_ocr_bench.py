"""Compares local MLX vision models with the production OCR on one real video.

Three scenarios, each a model for `detect_region` (where the subtitle band is, asked of
every sampled frame) and one for `read_subtitles` (the subtitle text, asked of a few):

    paddle   PaddleOCR-VL 1.6   detect + read
    qwen8b   Qwen3-VL-8B        detect + read
    mixed    Qwen3-VL-2B detect, PaddleOCR-VL read

Qwen3-VL is asked through the production `VisionOcr` (same prompts, same reply parsing,
same box format) against a local `mlx_vlm.server`, which this script starts and stops.

PaddleOCR-VL follows no instruction: it knows fixed task prompts only, ignores the
production prompt (it copies the frame's text instead of a box), and mlx-vlm fails on a
request with more than one of its images. It gets the adapter a provider would need:
`Spotting:` lists every text line with its corners (0..1000 of each side), and the box
is the union of the centred lines, by the production rule (`MAX_CENTRE_OFFSET`), so the
name cards and logos it also lists are left out; reading asks `OCR:` of each frame's
subtitle strip, one frame a request.

The reference is the production run's subtitle document (OpenRouter
`deepseek/deepseek-v4.1-flash`): its per-frame boxes and its band. Nothing here is a
human label, so "agreement" means "agrees with the model in production".

    # once: the reference document of a finished run
    docker exec -i reup-flow-worker-agents-1 python -c "import sys; \\
      from localization.config import load_settings; \\
      from localization.storage.object_store import ObjectStore; \\
      sys.stdout.buffer.write(ObjectStore.from_settings(load_settings()).get_bytes( \\
      'work/7692039666727472434/g4/r0/subtitles.json', produced_by='detect_subtitles'))" \\
      > ../../data/ocr-bench/7692039666727472434-subtitles.json

    uv run python eval/local_ocr_bench.py \\
        --video ../../data/review/7692039666727472434/g3/<sha>.mp4 \\
        --reference ../../data/ocr-bench/7692039666727472434-subtitles.json \\
        --scenario all --out ../../data/ocr-bench/7692039666727472434

Per scenario it writes `<out>/<scenario>.json` (every answer, with the model's raw reply)
and prints, for detection: seconds per frame, how the answers split (box, none,
unusable, error), agreement with the reference on whether a subtitle is shown, and
the IoU of the boxes where both saw one; for reading: seconds per frame, the share of
calls whose reply was valid, and the share of subtitle frames read as empty. The
texts are in the JSON, to read beside the frames.
"""

import argparse
import asyncio
import base64
import json
import re
import shlex
import socket
import statistics
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from localization.activities.detect_subtitles import (
    FRAME_INTERVAL_MS,
    MAX_CENTRE_OFFSET,
    SAMPLE_FPS,
    ocr_size,
    to_source,
)
from localization.activities.ffmpeg import extract_frames, probe_video
from localization.activities.source_evidence import source_frame, subtitle_band
from localization.contracts import BBox, SubtitleDocument
from localization.contracts.source_evidence import SubtitleReading, SubtitleReadings
from localization.providers.errors import ProviderError, TransientProviderError, UnusableAnswer
from localization.providers.vision_ocr import VisionOcr

PADDLE = "mlx-community/PaddleOCR-VL-1.6-4bit"
QWEN_8B = "mlx-community/Qwen3-VL-8B-Instruct-4bit"
QWEN_2B = "mlx-community/Qwen3-VL-2B-Instruct-4bit"
# scenario -> (detect model, read model)
SCENARIOS = {
    "paddle": (PADDLE, PADDLE),
    "qwen8b": (QWEN_8B, QWEN_8B),
    "mixed": (QWEN_2B, PADDLE),
}
# The production box format: Qwen3-VL grounds in 0..1000 of each side, as deepseek does.
BBOX_FORMAT = "xyxy_norm1000"
SERVER_CMD = "uvx --from mlx-vlm==0.7.12 python -m mlx_vlm.server"
# Production reads up to eight frames a call; four keeps a slow model's call short.
READ_BATCH = 4
# A first request downloads and loads the model: gigabytes for the 8B.
TIMEOUT_SECONDS = 1800.0


@dataclass
class DetectAnswer:
    at_ms: int
    reference: list[int] | None
    outcome: str  # box | none | unusable | error
    box: list[int] | None
    iou: float | None
    seconds: float
    reply: str


@dataclass
class ReadAnswer:
    times_ms: list[int]
    valid: bool
    texts: list[str]
    seconds: float
    reply: str


@dataclass
class ScenarioResult:
    scenario: str
    detect_model: str
    read_model: str
    detect: list[DetectAnswer] = field(default_factory=list)
    read: list[ReadAnswer] = field(default_factory=list)


class LastReply:
    """Keeps the last chat completion's text, which `VisionOcr` parses and drops: an
    unusable answer is only understood by reading it."""

    def __init__(self) -> None:
        self.text = ""

    async def hook(self, response: httpx.Response) -> None:
        await response.aread()
        try:
            self.text = response.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            self.text = response.text[:500]


def iou(a: BBox, b: BBox) -> float:
    ix = max(0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union else 0.0


def in_band(box: BBox | None, region: BBox | None) -> bool:
    """The reference box is the subtitle line (inside the band), not other text."""
    if box is None or region is None:
        return False
    return box.y < region.y + region.h and region.y < box.y + box.h


def spread(items: list[int], count: int) -> list[int]:
    """`count` items evenly spaced over `items`, first and last included."""
    if count >= len(items):
        return items
    step = (len(items) - 1) / max(1, count - 1)
    return [items[round(i * step)] for i in range(count)]


def pick_frames(doc: SubtitleDocument, count: int) -> list[int]:
    """Frame indices: half where the reference saw the subtitle line, half where it saw
    no text at all, spread over the whole video so scenes vary."""
    boxes = doc.frame_boxes
    shown = [i for i, b in enumerate(boxes) if in_band(b, doc.region)]
    empty = [i for i, b in enumerate(boxes) if b is None]
    return sorted(spread(shown, count // 2) + spread(empty, count - count // 2))


def read_times(doc: SubtitleDocument, count: int) -> list[int]:
    """Times (ms) of frames where the reference saw the subtitle line, offset from the
    detection frames so reading is not judged on the same pictures."""
    shown = [i for i, b in enumerate(doc.frame_boxes) if in_band(b, doc.region)]
    picked = spread(shown[len(shown) // (2 * count) :], count)
    return [i * doc.frame_interval_ms for i in picked]


# One spotted line: its text, then the four corners as eight 0..1000 numbers.
_SPOTTED = re.compile(r"([^\n<]*)((?:<\|LOC_\d+\|>){8})")
_LOC = re.compile(r"<\|LOC_(\d+)\|>")


class PaddleOcr:
    """`VisionOcr`'s two calls for PaddleOCR-VL, which only takes its task prompts."""

    def __init__(self, base_url: str, model: str, http: httpx.AsyncClient) -> None:
        self.model_id = model
        self._url = f"{base_url}/chat/completions"
        self._http = http

    async def _ask(self, image: bytes, task: str) -> str:
        data = base64.b64encode(image).decode()
        url = f"data:image/jpeg;base64,{data}"
        response = await self._http.post(
            self._url,
            json={
                "model": self.model_id,
                "max_tokens": 1000,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": url}},
                            {"type": "text", "text": task},
                        ],
                    }
                ],
            },
        )  # fmt: skip
        if response.status_code != 200:
            raise TransientProviderError(f"ocr: HTTP {response.status_code}")
        return response.json()["choices"][0]["message"]["content"] or ""

    async def detect_region(self, frame: bytes, width: int, height: int) -> BBox | None:
        reply = await self._ask(frame, "Spotting:")
        lines = []
        for _, corners in _SPOTTED.findall(reply):
            n = [int(v) for v in _LOC.findall(corners)]
            xs, ys = [v * width / 1000 for v in n[0::2]], [v * height / 1000 for v in n[1::2]]
            box = BBox(
                x=round(min(xs)), y=round(min(ys)),
                w=max(1, round(max(xs) - min(xs))), h=max(1, round(max(ys) - min(ys))),
            )  # fmt: skip
            if abs(box.x + box.w / 2 - width / 2) <= MAX_CENTRE_OFFSET * width:
                lines.append(box)
        if not lines and reply.strip() and not _SPOTTED.search(reply):
            raise UnusableAnswer("ocr: no spotted lines in the reply")
        if not lines:
            return None
        x1, y1 = min(b.x for b in lines), min(b.y for b in lines)
        x2, y2 = max(b.x + b.w for b in lines), max(b.y + b.h for b in lines)
        return BBox(x=x1, y=y1, w=x2 - x1, h=y2 - y1)

    async def read_subtitles(self, samples: list[tuple[int, bytes]]) -> SubtitleReadings:
        readings = []
        for time_ms, frame in samples:
            text = (await self._ask(frame, "OCR:")).strip()
            readings.append(SubtitleReading(time_ms=time_ms, text=text[:500]))
        return SubtitleReadings(readings=tuple(readings))


def make_ocr(base_url: str, model: str, reply: LastReply) -> VisionOcr | PaddleOcr:
    http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, event_hooks={"response": [reply.hook]})
    if model == PADDLE:
        return PaddleOcr(base_url, model, http)
    return VisionOcr(
        base_url, "local", model, BBOX_FORMAT, timeout_seconds=TIMEOUT_SECONDS, http=http
    )


async def detect(
    ocr: VisionOcr,
    reply: LastReply,
    doc: SubtitleDocument,
    video: Path,
    frames: list[int],
    tmp: Path,
) -> list[DetectAnswer]:
    source = (doc.width, doc.height)
    scaled = ocr_size(*source)
    answers = []
    for index in frames:
        at_ms = index * doc.frame_interval_ms
        out = tmp / f"detect-{index}"
        out.mkdir()
        (path,) = extract_frames(
            video, out, start_s=at_ms / 1000, count=1, fps=SAMPLE_FPS,
            width=scaled[0], height=scaled[1],
        )  # fmt: skip
        image = path.read_bytes()
        reference = doc.frame_boxes[index]
        reply.text = ""
        started = time.perf_counter()
        try:
            found = to_source(await ocr.detect_region(image, *scaled), scaled, source)
            outcome = "box" if found else "none"
        except UnusableAnswer:
            found, outcome = None, "unusable"
        except (ProviderError, httpx.HTTPError):
            found, outcome = None, "error"
        seconds = time.perf_counter() - started
        answers.append(
            DetectAnswer(
                at_ms=at_ms,
                reference=_xywh(reference),
                outcome=outcome,
                box=_xywh(found),
                iou=iou(found, reference) if found and reference else None,
                seconds=round(seconds, 3),
                reply=reply.text[:300],
            )
        )
        print(f"  detect {at_ms:>7} ms  {outcome:<8} {seconds:5.2f} s", flush=True)
    return answers


async def read(
    ocr: VisionOcr,
    reply: LastReply,
    doc: SubtitleDocument,
    video: Path,
    times: list[int],
    tmp: Path,
) -> list[ReadAnswer]:
    band = subtitle_band(doc)
    answers = []
    for start in range(0, len(times), READ_BATCH):
        batch = times[start : start + READ_BATCH]
        samples = [(t, source_frame(video, t, tmp / f"read-{t}.jpg", band)) for t in batch]
        reply.text = ""
        started = time.perf_counter()
        try:
            result = await ocr.read_subtitles(samples)
            valid, texts = True, [r.text for r in result.readings]
        except (ProviderError, httpx.HTTPError):
            valid, texts = False, []
        seconds = time.perf_counter() - started
        answers.append(ReadAnswer(batch, valid, texts, round(seconds, 3), reply.text[:1000]))
        print(f"  read {batch[0]:>7} ms ×{len(batch)}  valid={valid}  {seconds:5.2f} s", flush=True)
    return answers


def _xywh(box: BBox | None) -> list[int] | None:
    return [box.x, box.y, box.w, box.h] if box else None


async def warm_up(ocr: VisionOcr, video: Path, doc: SubtitleDocument, tmp: Path) -> None:
    """One untimed request, so the timings leave out the download and the load."""
    print(f"  warming up {ocr.model_id} (downloads it the first time)", flush=True)
    image = source_frame(video, 0, tmp / "warm.jpg")
    try:
        await ocr.detect_region(image, *ocr_size(doc.width, doc.height))
    except ProviderError:
        pass  # an unusable answer still loaded the model


async def run_scenario(
    name: str, base_url: str, doc: SubtitleDocument, video: Path, frames: list[int],
    times: list[int], tmp: Path,
) -> ScenarioResult:  # fmt: skip
    detect_model, read_model = SCENARIOS[name]
    result = ScenarioResult(name, detect_model, read_model)
    print(f"\n== {name}: detect {detect_model}, read {read_model}", flush=True)
    # Detection first, then reading: one model switch at most, done outside the timings.
    reply = LastReply()
    ocr = make_ocr(base_url, detect_model, reply)
    await warm_up(ocr, video, doc, tmp)
    result.detect = await detect(ocr, reply, doc, video, frames, tmp)
    if read_model != detect_model:
        ocr = make_ocr(base_url, read_model, reply)
        await warm_up(ocr, video, doc, tmp)
    result.read = await read(ocr, reply, doc, video, times, tmp)
    return result


def summary(result: ScenarioResult, total_frames: int) -> dict:
    det, rd = result.detect, result.read
    seconds = [a.seconds for a in det]
    outcomes = {k: sum(a.outcome == k for a in det) for k in ("box", "none", "unusable", "error")}
    judged = [a for a in det if a.outcome in ("box", "none")]
    agree = sum((a.outcome == "box") == (a.reference is not None) for a in judged)
    ious = [a.iou for a in det if a.iou is not None]
    read_frames = sum(len(a.times_ms) for a in rd)
    read_seconds = sum(a.seconds for a in rd)
    texts = [t for a in rd for t in a.texts]
    median = statistics.median(seconds) if seconds else 0.0
    return {
        "scenario": result.scenario,
        "detect_model": result.detect_model,
        "read_model": result.read_model,
        "detect_s_per_frame_median": round(median, 2),
        "detect_s_per_frame_p90": round(sorted(seconds)[int(0.9 * (len(seconds) - 1))], 2)
        if seconds else None,
        "detect_whole_video_min_estimate": round(median * total_frames / 60, 1),
        "detect_outcomes": outcomes,
        "presence_agreement": round(agree / len(judged), 3) if judged else None,
        "box_iou_median": round(statistics.median(ious), 3) if ious else None,
        "box_iou_p10": round(sorted(ious)[int(0.1 * (len(ious) - 1))], 3) if ious else None,
        "read_s_per_frame": round(read_seconds / read_frames, 2) if read_frames else None,
        "read_valid_calls": f"{sum(a.valid for a in rd)}/{len(rd)}",
        "read_empty_on_subtitle_frames": round(sum(not t.strip() for t in texts) / len(texts), 3)
        if texts else None,
    }  # fmt: skip


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start_server(cmd: str, port: int, log: Path) -> subprocess.Popen:
    if not port_free(port):
        raise SystemExit(
            f"port {port} is in use; stop what holds it (lsof -i :{port}) or pass --port"
        )
    process = subprocess.Popen(
        [*shlex.split(cmd), "--host", "127.0.0.1", "--port", str(port)],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 600  # the first uvx run installs mlx-vlm
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"mlx_vlm.server exited ({process.returncode}); see {log}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                return process
        except httpx.HTTPError:
            pass
        time.sleep(2)
    process.terminate()
    raise SystemExit(f"mlx_vlm.server did not answer /health within 10 minutes; see {log}")


async def main_async(args: argparse.Namespace) -> int:
    doc = SubtitleDocument.model_validate_json(Path(args.reference).read_text())
    if doc.frame_interval_ms != FRAME_INTERVAL_MS:
        raise SystemExit(
            f"reference sampled every {doc.frame_interval_ms} ms, not {FRAME_INTERVAL_MS}"
        )
    video = Path(args.video)
    info = probe_video(video)
    if (info.width, info.height) != (doc.width, doc.height):
        raise SystemExit("the reference was made on a video of another size")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frames = pick_frames(doc, args.frames)
    times = read_times(doc, args.reads)
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    server = start_server(args.server_cmd, args.port, out / "mlx-server.log")
    rows = []
    try:
        base_url = f"http://127.0.0.1:{args.port}/v1"
        for name in names:
            with tempfile.TemporaryDirectory(prefix=f"ocr-bench-{name}-") as tmp:
                result = await run_scenario(name, base_url, doc, video, frames, times, Path(tmp))
            (out / f"{name}.json").write_text(
                json.dumps(asdict(result), ensure_ascii=False, indent=1)
            )
            rows.append(summary(result, len(doc.frame_boxes)))
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
    (out / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print("\n" + json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--video", required=True, help="the source video of the reference run")
    parser.add_argument("--reference", required=True, help="that run's subtitles.json")
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--frames", type=int, default=40, help="frames asked for a box")
    parser.add_argument("--reads", type=int, default=24, help="subtitle frames read")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--server-cmd", default=SERVER_CMD)
    parser.add_argument("--out", required=True, help="directory for the results")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
