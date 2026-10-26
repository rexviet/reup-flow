"""Measuring audio in tests without trusting the code under test: ffmpeg decodes,
plain Python measures."""

import math
import re
import subprocess
from array import array
from pathlib import Path

RATE = 8000


def decode(path: Path) -> array:
    """Mono 8 kHz signed 16-bit samples."""
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(RATE)]
        + ["-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    samples = array("h")
    samples.frombytes(raw)
    return samples


def level(samples: array, start_ms: int, end_ms: int, frequency: float) -> float:
    """Amplitude (0..1) of one frequency in a window, by the Goertzel algorithm."""
    window = samples[start_ms * RATE // 1000 : end_ms * RATE // 1000]
    if not window:
        return 0.0
    coeff = 2 * math.cos(2 * math.pi * frequency / RATE)
    s1 = s2 = 0.0
    for x in window:
        s1, s2 = x / 32768 + coeff * s1 - s2, s1
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return 2 * math.sqrt(max(power, 0.0)) / len(window)


def db(ratio: float) -> float:
    return 20 * math.log10(max(ratio, 1e-9))


def tone_span_ms(samples: array, frequency: float, *, step_ms: int = 20, floor: float = 0.01):
    """(first, last) millisecond at which `frequency` is present, in `step_ms` steps."""
    hits = [
        t
        for t in range(0, len(samples) * 1000 // RATE - step_ms, step_ms)
        if level(samples, t, t + step_ms, frequency) > floor
    ]
    return (hits[0], hits[-1] + step_ms) if hits else None


def integrated_lufs(path: Path) -> float:
    """EBU R128 integrated loudness, measured by ffmpeg's ebur128 filter."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True,
        check=True,
    ).stderr.decode()
    summary = out[out.rindex("Summary:") :]
    return float(re.search(r"I:\s+(-?[0-9.]+) LUFS", summary).group(1))


def true_peak_dbtp(path: Path) -> float:
    """Highest true peak (dBTP) of a file, measured by ffmpeg's ebur128 filter."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-nostats", "-i", str(path), "-af", "ebur128=peak=true"]
        + ["-f", "null", "-"],
        capture_output=True,
        check=True,
    ).stderr.decode()
    summary = out[out.rindex("Summary:") :]
    return float(re.search(r"Peak:\s+(-?[0-9.]+) dBFS", summary).group(1))
