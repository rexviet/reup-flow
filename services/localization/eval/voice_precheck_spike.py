"""Can a cheap check on the original audio tell, before demucs, that a video has no voice?

    uv run --with silero-vad --with webrtcvad-wheels --with numpy \
        python eval/voice_precheck_spike.py labels.json data/douyin/<channel>

`labels.json` maps an aweme id to "speech" or "none": what the full pipeline's speech-to-text
found on the separated vocals (zero segments means none). For each labelled video the script
decodes the audio to 16 kHz mono and measures what share of it two voice-activity detectors
call speech, so the two groups can be compared: a threshold is usable only if every "speech"
video sits above it and the "none" videos below it.
"""

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import webrtcvad
from silero_vad import get_speech_timestamps, load_silero_vad

RATE = 16000


def decode(video: Path, out: Path) -> float | None:
    """Mono 16 kHz PCM of the first audio stream; None when the file has no audio."""
    run = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", str(RATE), "-f", "wav", str(out)],
        capture_output=True,
    )
    if run.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        return None
    with wave.open(str(out)) as w:
        return w.getnframes() / w.getframerate()


def load(out: Path) -> np.ndarray:
    with wave.open(str(out)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def silero_share(model, samples: np.ndarray, seconds: float) -> float:
    import torch

    audio = torch.from_numpy(samples.astype(np.float32) / 32768.0)
    spans = get_speech_timestamps(audio, model, sampling_rate=RATE, threshold=0.5, min_speech_duration_ms=250)
    return sum(s["end"] - s["start"] for s in spans) / RATE / seconds


def webrtc_share(samples: np.ndarray, mode: int = 3) -> float:
    vad = webrtcvad.Vad(mode)
    frame = int(RATE * 0.03)
    flags = [vad.is_speech(samples[i : i + frame].tobytes(), RATE) for i in range(0, len(samples) - frame, frame)]
    return sum(flags) / max(len(flags), 1)


def main() -> int:
    labels = json.load(open(sys.argv[1]))
    folder = Path(sys.argv[2])
    model = load_silero_vad()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for aweme, label in sorted(labels.items(), key=lambda kv: (kv[1], kv[0])):
            video = folder / f"{aweme}.mp4"
            if not video.exists():
                continue
            wav = Path(tmp) / "a.wav"
            seconds = decode(video, wav)
            if seconds is None or seconds < 0.5:
                rows.append((label, aweme, None, None, None))
                continue
            samples = load(wav)
            rows.append((label, aweme, seconds, silero_share(model, samples, seconds), webrtc_share(samples)))
    print(f"{'truth':7} {'video':8} {'secs':>5} {'silero%':>8} {'webrtc%':>8}")
    for label, aweme, seconds, s, w in rows:
        if seconds is None:
            print(f"{label:7} {aweme[-6:]:8} no audio stream")
        else:
            print(f"{label:7} {aweme[-6:]:8} {seconds:5.0f} {100 * s:8.1f} {100 * w:8.1f}")
    for name, idx in (("silero", 3), ("webrtc", 4)):
        for label in ("speech", "none"):
            vals = [r[idx] for r in rows if r[0] == label and r[idx] is not None]
            if vals:
                print(f"{name:7} {label:7} n={len(vals):2d} min={100 * min(vals):5.1f}% max={100 * max(vals):5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
