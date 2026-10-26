"""ffprobe / ffmpeg wrappers shared by the media activities.

Every command is an argv list (no shell). A child sees only what it needs to run,
so a credential in the worker's environment never reaches it.
"""

import asyncio
import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity

from localization.providers.errors import PermanentProviderError, TransientProviderError

_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
HEARTBEAT_SECONDS = 10.0


def _child_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: int
    duration_ms: int
    has_audio: bool


def _run(argv: list[str], *, what: str, timeout: float) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False, env=_child_env()
        )
    except subprocess.TimeoutExpired:
        raise TransientProviderError(f"{what}: timeout") from None
    except OSError:
        raise PermanentProviderError(f"{what}: binary could not be started") from None
    return result


def _fps(stream: dict) -> int:
    for field in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(stream.get(field, "0/0")).partition("/")
        try:
            value = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return max(1, round(value))
    return 30


def parse_probe(body: object) -> VideoInfo:
    """A video stream that is only cover art (an MP3 with a picture, which is what
    an image post with a music track looks like) is not a video stream."""
    if not isinstance(body, dict):
        raise PermanentProviderError("ffprobe: unexpected output")
    streams = [s for s in body.get("streams", []) if isinstance(s, dict)]
    videos = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not videos:
        raise PermanentProviderError("no_video_stream")
    video = videos[0]
    try:
        width, height = int(video["width"]), int(video["height"])
        duration = float((body.get("format") or {}).get("duration") or video.get("duration") or 0)
    except (KeyError, TypeError, ValueError):
        raise PermanentProviderError("ffprobe: video stream has no usable size") from None
    if width <= 0 or height <= 0:
        raise PermanentProviderError("ffprobe: video stream has no usable size")
    return VideoInfo(
        width=width,
        height=height,
        fps=_fps(video),
        duration_ms=max(0, round(duration * 1000)),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def probe_video(path: Path) -> VideoInfo:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        what="ffprobe",
        timeout=60,
    )
    if result.returncode != 0:
        raise PermanentProviderError("ffprobe: the file could not be read")
    try:
        body = json.loads(result.stdout)
    except ValueError:
        raise PermanentProviderError("ffprobe: unexpected output") from None
    return parse_probe(body)


def probe_duration_ms(path: Path) -> int:
    """Length of an audio file, from its container."""
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],  # fmt: skip
        what="ffprobe",
        timeout=60,
    )
    try:
        seconds = float(result.stdout.decode().strip())
    except ValueError:
        seconds = 0.0
    if result.returncode != 0 or seconds <= 0:
        raise PermanentProviderError("ffprobe: the audio has no measurable length")
    return round(seconds * 1000)


def run_ffmpeg(
    args: list[str], *, timeout: float = 900, loglevel: str = "error"
) -> subprocess.CompletedProcess[bytes]:
    """`ffmpeg -nostdin -y` with `args`. A non-zero exit is a permanent failure: the
    same inputs fail the same way."""
    result = _run(
        ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-v", loglevel, *args],
        what="ffmpeg",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise PermanentProviderError("ffmpeg: the command failed")
    return result


def extract_audio(video: Path, out_wav: Path) -> None:
    result = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            str(out_wav),
        ],  # fmt: skip
        what="ffmpeg",
        timeout=600,
    )
    if result.returncode != 0 or not out_wav.exists():
        raise PermanentProviderError("no_audio_track")


def extract_frames(
    video: Path,
    out_dir: Path,
    *,
    start_s: float,
    count: int,
    fps: float,
    width: int,
    height: int,
    at_end: bool = False,
) -> list[Path]:
    """At most `count` JPEG frames sampled at `fps` from `start_s`, already scaled to
    `width` x `height`. Small on purpose: the caller extracts a chunk at a time, so
    the disk holds a bounded number of small files, never the whole video."""
    result = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{start_s:.3f}",
            "-i",
            str(video),
            "-vf",
            f"fps={fps},scale={width}:{height}",
            "-frames:v",
            str(count),
            "-an",
            "-q:v",
            "4",
            str(out_dir / "frame-%06d.jpg"),
        ],  # fmt: skip
        what="ffmpeg",
        timeout=300,
    )
    frames = sorted(out_dir.glob("frame-*.jpg"))
    # Seeking to the end of the video writes nothing and ffmpeg exits non-zero. That
    # is the end of the video only when the caller says it is at the end.
    if result.returncode != 0 and not (at_end and not frames):
        raise PermanentProviderError("ffmpeg: frames could not be extracted")
    return frames


def to_stt_audio(source: Path, out_wav: Path) -> None:
    """16 kHz mono: what speech models consume, about a tenth of a 44.1 kHz stereo
    stem, so a long video stays under a server's upload limit."""
    result = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(out_wav),
        ],  # fmt: skip
        what="ffmpeg",
        timeout=600,
    )
    if result.returncode != 0 or not out_wav.exists():
        raise PermanentProviderError("ffmpeg: the audio could not be converted for STT")


async def run_blocking(
    fn, *args, heartbeat_every: float | None = None, cancel: threading.Event | None = None
):
    """Runs blocking work in a thread and heartbeats while it runs, so a stuck
    activity is detected by its heartbeat timeout rather than only at the end. A
    thread cannot be interrupted, so on cancellation `cancel` is set: work that
    owns a child process watches it and kills the child."""
    interval = HEARTBEAT_SECONDS if heartbeat_every is None else heartbeat_every
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                return task.result()
            activity.heartbeat()
    except asyncio.CancelledError:
        if cancel is not None:
            cancel.set()
        raise
