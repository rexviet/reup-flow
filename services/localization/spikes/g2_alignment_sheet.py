"""G2 manual step: build a page for hand-checking 10 sentence timestamps (<= 200 ms).

Run on the host (needs ffmpeg and curl; stdlib only):
  set -a; . ../../../.env; set +a
  python3 g2_alignment_sheet.py
Output: out/g2-alignment/index.html (open it in a browser).

For each sentence it cuts the audio twice: EXACT = [start, end] as the server
reported them, and PADDED = the same window widened by 200 ms on each side.
If EXACT clips a syllable but PADDED has the whole sentence, the drift is
inside 200 ms (pass). If PADDED still clips speech, or EXACT begins/ends with
long silence or another sentence, the drift is too big (fail).
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

URL = os.environ["VOICESTUDIO_URL"].rstrip("/")
KEY = os.environ["VOICESTUDIO_API_KEY"]
MODEL = os.environ.get("STT_MODEL", "whisper-1")
LANG = os.environ.get("G2_STT_LANGUAGE", "zh")
DATA = Path(__file__).resolve().parents[3] / "data"
OUT = Path(__file__).resolve().parent / "out" / "g2-alignment"
PAD = 0.2
SENTENCES = 10
MIN_LEN, MAX_LEN, GAP = 1.5, 8.0, 0.35
CANDIDATES = 30
SPEECH_CLIPS = 5
CLIP_SECONDS = 60


def transcribe(wav: Path) -> dict:
    out = subprocess.run(
        ["curl", "-s", "-m", "280", "-H", f"Authorization: Bearer {KEY}", "-F", f"file=@{wav}",
         "-F", f"model={MODEL}", "-F", f"language={LANG}", "-F", "response_format=verbose_json",
         "-F", "timestamp_granularities[]=word", f"{URL}/v1/audio/transcriptions"],
        capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def extract(video: Path, wav: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-t", str(CLIP_SECONDS),
                    "-ac", "1", "-ar", "16000", str(wav)], check=True)


def cut(src: Path, start: float, end: float, dst: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, start):.3f}", "-to", f"{end:.3f}",
                    "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)], check=True)


def chunks(words: list[dict]) -> list[dict]:
    """Group words into sentence-like chunks: split at a pause or at MAX_LEN."""
    out, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > GAP or w["end"] - cur[0]["start"] > MAX_LEN):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return [
        {"start": c[0]["start"], "end": c[-1]["end"], "text": "".join(x["word"] for x in c)}
        for c in out
        if MIN_LEN <= c[-1]["end"] - c[0]["start"] <= MAX_LEN + 1
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    all_videos = sorted(DATA.rglob("*.mp4"))
    candidates = all_videos[:: max(1, len(all_videos) // CANDIDATES)][:CANDIDATES]
    speech, silent = [], []
    for v in candidates:
        if len(speech) >= SPEECH_CLIPS:
            break
        wav = OUT / f"{v.stem}.wav"
        extract(v, wav)
        body = transcribe(wav)
        if re.sub(r"[\s，。！？、,.!?]+", "", body.get("text", "")) and body.get("words"):
            speech.append((v, wav, body))
        else:
            silent.append((v, wav))
    if len(speech) < 2:
        print("not enough clips with speech", file=sys.stderr)
        return 1

    per_clip = [chunks(b["words"]) for _, _, b in speech]
    picked, i = [], 0
    while len(picked) < SENTENCES and any(per_clip):
        c = per_clip[i % len(per_clip)]
        if c:
            # spread across the clip rather than taking the first sentences
            picked.append((speech[i % len(speech)], c.pop(len(c) // 2)))
        i += 1

    rows = []
    for n, ((v, wav, _), s) in enumerate(picked, 1):
        exact, padded = OUT / f"s{n:02d}-exact.wav", OUT / f"s{n:02d}-padded.wav"
        cut(wav, s["start"], s["end"], exact)
        cut(wav, s["start"] - PAD, s["end"] + PAD, padded)
        rows.append(
            f'<section><h3>#{n} <small>{v.stem[-8:]} · {s["start"]:.2f}s to {s["end"]:.2f}s</small></h3>'
            f'<p class="t">{html.escape(s["text"])}</p>'
            f'<div><b>EXACT</b> <audio controls preload="auto" src="{exact.name}"></audio></div>'
            f'<div><b>PADDED ±200 ms</b> <audio controls preload="auto" src="{padded.name}"></audio></div>'
            f'<label><input type="radio" name="r{n}" value="pass"> PASS</label> '
            f'<label><input type="radio" name="r{n}" value="fail"> FAIL</label></section>'
        )

    extras = []
    for v, wav in silent[:2]:
        short = OUT / f"silent-{v.stem[-8:]}.wav"
        cut(wav, 0, 20, short)
        extras.append(f'<div><b>{v.stem[-8:]}</b> <audio controls src="{short.name}"></audio></div>')

    (OUT / "index.html").write_text(f"""<!doctype html><meta charset="utf-8"><title>G2 alignment</title>
<style>body{{font:16px system-ui;max-width:760px;margin:24px auto;padding:0 16px}}
section{{border:1px solid #ccc;border-radius:8px;padding:12px;margin:12px 0}}
.t{{font-size:20px}} small{{color:#666;font-weight:400}} audio{{vertical-align:middle}}
#sum{{position:sticky;top:0;background:#fff;padding:8px 0;font-weight:600}}</style>
<h1>G2: timestamp check (10 sentences)</h1>
<p>For each sentence play <b>EXACT</b> then <b>PADDED</b>. <b>PASS</b>: the whole sentence is in PADDED and
EXACT is at most a hair off. <b>FAIL</b>: PADDED still cuts speech, or EXACT starts/ends with long silence
or another sentence. Gate passes at 10/10 within 200 ms, or tell me your threshold.</p>
<div id="sum">0 pass · 0 fail · 10 left</div>
{''.join(rows)}
<h2>Clips the server returned empty for (first 20 s)</h2>
<p>Is there speech in these? If yes, the server missed it; if only music, empty is correct.</p>
{''.join(extras)}
<script>
const s=document.getElementById('sum');
document.addEventListener('change',()=>{{
 const p=document.querySelectorAll('input[value=pass]:checked').length,
       f=document.querySelectorAll('input[value=fail]:checked').length;
 s.textContent=p+' pass · '+f+' fail · '+({len(rows)}-p-f)+' left';
}});
</script>""")
    print(f"{len(rows)} sentences, {len(silent[:2])} empty-clip samples -> {OUT / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
