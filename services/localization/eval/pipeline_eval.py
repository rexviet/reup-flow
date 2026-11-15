"""Scores whole runs against labels a person gave each video: what code can measure of the
failures seen on real videos.

    # E1 and E6, from the runs table and the run histories
    uv run python eval/pipeline_eval.py runs --labels labels.jsonl [--out report.json]

    # E2, on the audio of one run (the files named by `runs`, fetched from the bucket)
    uv run python eval/pipeline_eval.py vocals --output out.mp4 --vocals vocals.wav --bgm bgm.wav

Labels, one JSON object per line: {"aweme_id": "7565005737722907953", "audio": "singing"}
with `audio` one of speech, speech_over_music, singing, music, silence. The label is a
person's, from listening; nothing here guesses it.

  false_dub_rate      videos with no speech (singing, music, silence) that were dubbed
  missed_speech_rate  videos with speech that were not
  text_too_long_rate  runs that failed with TextTooLong
  max_line_bytes      the longest line's text as recorded with its clip (base64), against
                      the limit the TTS cache sets on it
  original_vocals_kept  how much of the source's own voice track is in the output, as the
                      ratio of the weights of `vocals` and `bgm` in a least-squares fit of
                      the output (no lag search: the render and the stems share a timeline)
                      (about 1 when the original voice stayed, about 0 when only the
                      background is left). Meaningful for a video that was not dubbed.

A video "was dubbed" when its transcript had segments: from there the run translates and
speaks every one of them.
"""

import argparse
import base64
import json
import math
import os
import subprocess
import sys
from array import array
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psycopg
from common import TEMPORAL_UI, History, host_database_url, load_env

from localization.activities.synthesize import MAX_METADATA_TEXT_BYTES

NO_SPEECH = {"singing", "music", "silence"}
SPEECH = {"speech", "speech_over_music"}
SAMPLE_RATE = 8000

LATEST_RUN_SQL = """
SELECT DISTINCT ON (aweme_id) aweme_id, generation, workflow_id, status, verdict
FROM localization.runs WHERE aweme_id = ANY(%s)
ORDER BY aweme_id, generation DESC
"""


@dataclass
class VideoRun:
    aweme_id: str
    audio: str
    generation: int | None = None
    status: str | None = None
    segments: int | None = None
    reasons: list[str] = field(default_factory=list)
    max_line_bytes: int | None = None
    vocals_key: str | None = None
    bgm_key: str | None = None
    output_key: str | None = None

    @property
    def dubbed(self) -> bool | None:
        return None if self.segments is None else self.segments > 0


def line_bytes(text: str) -> int:
    """The size the TTS cache records a line's text at (base64 of its UTF-8)."""
    return len(base64.b64encode(text.encode()))


def texts_in(history: History) -> Iterable[str]:
    """Every line text the run tried to speak: each translation, and each rewrite."""
    for args, _ in history.activities("assemble_script"):
        for line in (args[3] or {}).get("lines", []):
            yield line["text_vi"]
    for args, _ in history.activities("write_dubbed_script"):
        for arg in args:
            if isinstance(arg, dict) and "items" in arg:
                yield from (item["text_vi"] for item in arg["items"])


def fill_from_history(run: VideoRun, history: History) -> VideoRun:
    transcribed = history.activities("transcribe")
    if transcribed and transcribed[-1][1] is not None:
        run.segments = transcribed[-1][1]["segment_count"]
    ingested = history.activities("ingest")
    if ingested and ingested[-1][1] is not None:
        run.vocals_key = ingested[-1][1]["vocals_key"]
        run.bgm_key = ingested[-1][1]["bgm_key"]
    sizes = [line_bytes(t) for t in texts_in(history)]
    run.max_line_bytes = max(sizes) if sizes else None
    result = history.result() or {}
    published = result.get("publish") or {}
    run.output_key = published.get("output_key") or published.get("review_key")
    return run


def _rate(hits: list[bool]) -> float | None:
    return sum(hits) / len(hits) if hits else None


def summarize(runs: list[VideoRun]) -> dict:
    no_speech = [r for r in runs if r.audio in NO_SPEECH and r.dubbed is not None]
    speech = [r for r in runs if r.audio in SPEECH and r.dubbed is not None]
    finished = [r for r in runs if r.status is not None]
    sizes = [r.max_line_bytes for r in runs if r.max_line_bytes is not None]
    return {
        "videos": len(runs),
        "false_dub_rate": _rate([bool(r.dubbed) for r in no_speech]),
        "false_dubs": [r.aweme_id for r in no_speech if r.dubbed],
        "missed_speech_rate": _rate([not r.dubbed for r in speech]),
        "missed_speech": [r.aweme_id for r in speech if not r.dubbed],
        "text_too_long_rate": _rate(
            [any("TextTooLong" in reason for reason in r.reasons) for r in finished]
        ),
        "max_line_bytes": max(sizes) if sizes else None,
        "line_bytes_limit": MAX_METADATA_TEXT_BYTES,
    }


def read_labels(path: Path) -> list[VideoRun]:
    runs = []
    for line in path.read_text().splitlines():
        if line.strip():
            label = json.loads(line)
            if label["audio"] not in NO_SPEECH | SPEECH:
                raise ValueError(f"{label['aweme_id']}: unknown audio label {label['audio']!r}")
            runs.append(VideoRun(aweme_id=label["aweme_id"], audio=label["audio"]))
    return runs


def runs_command(args: argparse.Namespace) -> int:
    load_env()
    database_url = args.database_url or host_database_url(os.environ["LOC_DATABASE_URL"])
    runs = read_labels(Path(args.labels))
    by_id = {r.aweme_id: r for r in runs}
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(LATEST_RUN_SQL, (list(by_id),)).fetchall()
    for aweme_id, generation, workflow_id, status, record in rows:
        run = by_id[aweme_id]
        run.generation, run.status = generation, status
        run.reasons = list((record or {}).get("reasons") or [])
        fill_from_history(run, History.fetch(args.temporal_ui, workflow_id))
    summary = summarize(runs)
    report = {"summary": summary, "runs": [asdict(r) for r in runs]}
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    for run in runs:
        print(
            f"{run.aweme_id} g{run.generation} {run.status} audio={run.audio} "
            f"segments={run.segments} max_line_bytes={run.max_line_bytes}"
        )
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


# --- E2: is the source's voice still in the output ----------------------------------------


def pcm(path: Path, seconds: float | None = None) -> array:
    """Mono 16-bit samples at SAMPLE_RATE."""
    argv = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path)]
    if seconds is not None:
        argv += ["-t", str(seconds)]
    argv += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    samples = array("h")
    samples.frombytes(subprocess.run(argv, check=True, capture_output=True).stdout)
    return samples


def stem_weights(output: array, vocals: array, bgm: array) -> tuple[float, float]:
    """(a, b) of the least-squares fit output ~ a * bgm + b * vocals."""
    n = min(len(output), len(vocals), len(bgm))
    sbb = sbv = svv = sob = sov = 0.0
    for i in range(n):
        o, v, b = output[i], vocals[i], bgm[i]
        sbb += b * b
        sbv += b * v
        svv += v * v
        sob += o * b
        sov += o * v
    det = sbb * svv - sbv * sbv
    if det == 0:
        return 0.0, 0.0
    return (sob * svv - sov * sbv) / det, (sov * sbb - sob * sbv) / det


def original_vocals_kept(output: Path, vocals: Path, bgm: Path) -> float | None:
    """The weight of the vocals relative to the background in the output; None when the
    output has no background either (nothing to compare against)."""
    a, b = stem_weights(pcm(output), pcm(vocals), pcm(bgm))
    return None if abs(a) < 1e-6 else b / a


def rms(samples: array) -> float:
    return math.sqrt(sum(v * v for v in samples) / len(samples)) if samples else 0.0


def vocals_command(args: argparse.Namespace) -> int:
    kept = original_vocals_kept(Path(args.output), Path(args.vocals), Path(args.bgm))
    print(f"original_vocals_kept: {kept}")
    # A weight is only meaningful when the source had a voice to keep: a near-silent
    # vocals stem fits anything.
    vocals, bgm = rms(pcm(Path(args.vocals))), rms(pcm(Path(args.bgm)))
    print(f"vocals_rms: {vocals:.1f}  bgm_rms: {bgm:.1f}  (16-bit full scale is 32767)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    runs = sub.add_parser("runs", help="E1 and E6 over labelled videos")
    runs.add_argument("--labels", required=True)
    runs.add_argument("--out")
    runs.add_argument("--temporal-ui", default=TEMPORAL_UI)
    runs.add_argument("--database-url", help="defaults to LOC_DATABASE_URL on localhost")
    vocals = sub.add_parser("vocals", help="E2 on one run's audio")
    vocals.add_argument("--output", required=True)
    vocals.add_argument("--vocals", required=True)
    vocals.add_argument("--bgm", required=True)
    args = parser.parse_args()
    return runs_command(args) if args.command == "runs" else vocals_command(args)


if __name__ == "__main__":
    sys.exit(main())
