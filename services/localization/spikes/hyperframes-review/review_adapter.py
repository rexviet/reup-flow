"""Provider-free compatibility fixture; production adapters belong to later phases."""

import copy
import html
import json
import math
import re
import subprocess
import sys
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STYLE = {
    "color": r"#[0-9a-fA-F]{6}",
    "font-size": r"[1-9][0-9]{0,2}px",
    "font-family": r"[a-zA-Z0-9 ,_-]{1,100}",
    "font-weight": r"[1-9]00",
    "opacity": r"(?:0(?:\.[0-9]+)?|1(?:\.0+)?)",
    "-webkit-text-stroke": r"[0-9]{1,2}px #[0-9a-fA-F]{6}",
}


def project_path(root: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("outside project")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("outside project")
    return resolved


def authorize(host: str, origin: str | None, port: int) -> bool:
    # Exact authority, including port, defeats DNS rebinding and cross-site writes.
    authority = f"127.0.0.1:{port}"
    return host == authority and (origin is None or origin == f"http://{authority}")


def validate(document: dict) -> None:
    seen = set()
    for cue in document["cues"]:
        identity = cue["cueId"]
        if not re.fullmatch(r"cue-[a-z0-9-]+", identity) or identity in seen:
            raise ValueError("invalid or duplicate cueId")
        seen.add(identity)
        for start, end in [("start_ms", "end_ms"), ("source_start_ms", "source_end_ms")]:
            if not 0 <= cue[start] < cue[end] <= document["duration_ms"]:
                raise ValueError("invalid cue timing")
        if not isinstance(cue["text"], str) or not isinstance(cue["reason"], str):
            raise ValueError("text and reason must be literal strings")
        if not cue["text"].strip() and not cue["reason"].strip():
            raise ValueError("blank cue needs a reason")
        for key, value in cue["style"].items():
            if key not in STYLE or not re.fullmatch(STYLE[key], value):
                raise ValueError("unsupported visual style")


def merge_visual(document: dict, visual: dict) -> dict:
    result = copy.deepcopy(document)
    by_id = {c["cueId"]: c for c in result["cues"]}
    for identity, style in visual.items():
        if identity not in by_id:
            raise ValueError("unknown cue identity")
        by_id[identity]["style"].update(style)
    validate(result)
    result["revision"] += 1
    return result


def timestamp(ms: int) -> str:
    h, rest = divmod(ms, 3600000)
    m, rest = divmod(rest, 60000)
    s, milli = divmod(rest, 1000)
    return f"{h:02}:{m:02}:{s:02},{milli:03}"


def export_srt(document: dict) -> str:
    validate(document)
    blocks = []
    for cue in document["cues"]:
        if cue["text"].strip():
            if "\n\n" in cue["text"] or "\r" in cue["text"]:
                raise ValueError("SRT cannot preserve blank lines inside a cue")
            blocks.append(
                f"{len(blocks) + 1}\n{timestamp(cue['start_ms'])} --> "
                f"{timestamp(cue['end_ms'])}\n{cue['text']}\n"
            )
    return "\n".join(blocks)


def import_srt(document: dict, source: str) -> dict:
    """Only exact unique timing anchors can recover identity; sequence is discarded."""
    result = copy.deepcopy(document)
    anchors = {}
    for cue in result["cues"]:
        key = (cue["start_ms"], cue["end_ms"])
        anchors.setdefault(key, []).append(cue)
    used = set()
    normalized = source.replace("\r\n", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    for block in normalized.split("\n\n"):
        if not block:
            continue
        lines = block.split("\n")
        if len(lines) < 3 or not lines[0].isdigit():
            raise ValueError("invalid SRT block")
        match = re.fullmatch(
            r"(\d{2,}):(\d{2}):(\d{2}),(\d{3}) --> "
            r"(\d{2,}):(\d{2}):(\d{2}),(\d{3})",
            lines[1],
        )
        if not match:
            raise ValueError("invalid SRT timing")
        values = list(map(int, match.groups()))
        if any(values[i] >= 60 for i in (1, 2, 5, 6)):
            raise ValueError("invalid SRT timestamp")
        times = tuple(
            ((values[i] * 60 + values[i + 1]) * 60 + values[i + 2]) * 1000 + values[i + 3]
            for i in (0, 4)
        )
        candidates = anchors.get(times, [])
        if len(candidates) != 1 or candidates[0]["cueId"] in used:
            raise ValueError("ambiguous SRT identity; explicit mapping required")
        cue = candidates[0]
        used.add(cue["cueId"])
        cue["text"] = "\n".join(lines[2:])
    if used != {c["cueId"] for c in result["cues"] if c["text"].strip()}:
        raise ValueError("missing SRT cue; explicit mapping required")
    result["revision"] += 1
    validate(result)
    return result


def capture_visual(path: Path, document: dict) -> dict:
    """Read Studio-saved inline styles by stable element ID; never read word positions."""
    identities = {cue["cueId"] for cue in document["cues"]}
    visual = {}

    class Parser(HTMLParser):
        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            identity = attrs.get("id")
            if identity not in identities:
                return
            if identity in visual:
                raise ValueError("duplicate stable element ID")
            style = {}
            for declaration in attrs.get("style", "").split(";"):
                key, sep, value = declaration.partition(":")
                if sep and key.strip() in STYLE:
                    style[key.strip()] = value.strip()
            visual[identity] = style

    Parser().feed(path.read_text())
    return visual


def regenerate(document: dict, root: Path) -> dict:
    source = project_path(root, "index.html")
    updated = (
        merge_visual(document, capture_visual(source, document)) if source.exists() else document
    )
    generate(updated, root)
    return updated


def generate(document: dict, root: Path) -> None:
    validate(document)
    srt = export_srt(document)

    def escape(value):
        return html.escape(str(value), quote=True)

    elements = []
    region = document.get("source_region")
    for cue in document["cues"]:
        if region:
            elements.append(
                f'<div id="mask-{cue["cueId"]}" class="clip source-mask" '
                f'data-start="{cue["source_start_ms"] / 1000}" '
                f'data-duration="{(cue["source_end_ms"] - cue["source_start_ms"]) / 1000}" '
                f'data-track-index="1" style="top:{int(region["y"])}px;'
                f'height:{int(region["h"])}px"></div>'
            )
        if not cue["text"].strip():
            continue
        style = ";".join(f"{k}:{v}" for k, v in cue["style"].items())
        elements.append(
            f'<div id="{cue["cueId"]}" class="clip caption" '
            f'data-start="{cue["start_ms"] / 1000}" '
            f'data-duration="{(cue["end_ms"] - cue["start_ms"]) / 1000}" '
            f'data-track-index="2" style="{escape(style)}">{escape(cue["text"])}</div>'
        )
    template = (ROOT / "project-template.html").read_text()
    output = template.replace("{{ELEMENTS}}", "\n".join(elements))
    for key in ("width", "height", "duration_ms"):
        value = document[key] / 1000 if key == "duration_ms" else document[key]
        output = output.replace("{{" + key + "}}", str(value))
    sidecar = json.dumps(document, ensure_ascii=False, indent=2)
    root.mkdir(parents=True, exist_ok=True)
    project_path(root, "index.html").write_text(output)
    # JSON is external data, never interpolated into an executable script or innerHTML.
    project_path(root, "cues.json").write_text(sidecar)
    project_path(root, "captions.srt").write_text(srt)


def duration_ms(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = float(result.stdout) * 1000
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid audio duration")
    return value


def revoice(cue: dict, candidate: Path, output: Path) -> dict:
    result = copy.deepcopy(cue)
    result["mismatch"] = True
    slot = cue["end_ms"] - cue["start_ms"]
    try:
        measured = duration_ms(candidate)
        if slot <= 0 or measured > slot * 1.15:
            return result
        selected = candidate
        if measured > slot:
            if output.resolve() in {
                candidate.resolve(),
                Path(cue.get("active_clip", "")).resolve(),
            }:
                raise ValueError("stretch must create a new clip")
            ratio = measured / slot
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-n",
                    "-i",
                    str(candidate),
                    "-af",
                    f"atempo={ratio:.9f}",
                    str(output),
                ],
                check=True,
                capture_output=True,
            )
            if duration_ms(output) > slot + 50:
                return result
            selected = output
        result.update(active_clip=str(selected), spoken_text=cue["text"], mismatch=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def bridge(root: Path, port: int = 18441) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if not authorize(self.headers.get("Host", ""), self.headers.get("Origin"), port):
                self.send_error(403)
                return
            if self.path != "/visual":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if not 0 < length <= 65536 or self.headers.get("Content-Type") != "application/json":
                self.send_error(400)
                return
            try:
                patch = json.loads(self.rfile.read(length))
                target = project_path(root, "cues.json")
                document = json.loads(target.read_text())
                if patch["revision"] != document["revision"]:
                    self.send_error(409)
                    return
                updated = merge_visual(document, patch["visual"])
                generate(updated, root)
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"revision": updated["revision"]}).encode())

        def log_message(self, *_args):
            pass

    # Serialize writes in this fixture; production needs durable storage CAS.
    server = HTTPServer(("127.0.0.1", port), Handler)
    port = server.server_port
    return server


if __name__ == "__main__":
    if sys.argv[1:] == ["generate"]:
        regenerate(
            json.loads(
                (
                    ROOT / "project/cues.json"
                    if (ROOT / "project/cues.json").exists()
                    else ROOT / "fixtures/cues.json"
                ).read_text()
            ),
            ROOT / "project",
        )
    elif sys.argv[1:] == ["bridge"]:
        bridge(ROOT / "project").serve_forever()
    else:
        raise SystemExit("usage: review_adapter.py generate|bridge")
