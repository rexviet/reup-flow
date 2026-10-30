"""Step 7: render one mp4 for one spec, in one ffmpeg pass.

The output key carries the hash of the spec, `work/{aweme_id}/render-{spec_sha256}.mp4`.
If that object exists the spec has already been rendered and its key is returned: an
object appears whole or not at all, so there is no half-written state to find. Everything
else runs in a throw-away directory that is always removed.

The picture is what the spec says and nothing more: the source, muted, scaled to cover the
frame and black where it has no picture; the cover painted over the burned-in subtitle;
each caption, drawn once as an image (see `caption_images`), shown on exactly its frames;
the dubbed track as the only sound.

A render is bounded in time (a deadline that grows with the video), visibly alive (a
heartbeat while it works) and stoppable: cancelling the activity kills ffmpeg.
"""

import asyncio
import hashlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

from opentelemetry import trace
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.caption_images import (
    Cancelled,
    CaptionPainter,
    caption_track,
    frame_at,
    frame_count,
)
from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import HEARTBEAT_SECONDS, _child_env, run_blocking
from localization.activities.layout_captions import FontMeter
from localization.contracts import EditSpec, RenderInput, RenderResult
from localization.contracts.spec_hash import spec_sha256
from localization.observability.tracing import tag_session
from localization.providers.errors import PermanentProviderError, TransientProviderError
from localization.storage.object_store import ObjectStore, render_key

__all__ = [
    "RenderActivity",
    "check_font",
    "frame_at",
    "frame_count",
    "read_spec",
    "render_deadline_s",
    "render_file",
]

log = logging.getLogger(__name__)

# A render that has not finished by now is not going to. One pass was measured at about
# 0.13 s of wall clock per second of 1080x1920 video; the allowance is 15 times that, on
# top of a fixed time for moving files and drawing the captions.
RENDER_DEADLINE_BASE_S = 5 * 60
RENDER_DEADLINE_PER_VIDEO_SECOND = 2.0

VIDEO_CODEC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
AUDIO_CODEC = ["-c:a", "aac", "-b:a", "192k"]
_POLL_SECONDS = 0.2


def render_deadline_s(duration_ms: int) -> float:
    return RENDER_DEADLINE_BASE_S + RENDER_DEADLINE_PER_VIDEO_SECOND * duration_ms / 1000


def _refuse(message: str, kind: str) -> ApplicationError:
    """A refusal for good: the same input fails the same way."""
    return ApplicationError(message, type=kind, non_retryable=True)


def _under_video(key: str, aweme_id: str) -> bool:
    return key.startswith(f"work/{aweme_id}/") and not any(
        part.startswith("..") for part in key.split("/")
    )


def read_spec(raw: bytes) -> EditSpec:
    """The spec as stored by the layout stage, validated by the model that wrote it."""
    try:
        return EditSpec.model_validate_json(raw)
    except ValidationError as exc:
        raise _refuse(
            f"invalid EditSpec: {exc.error_count()} problem(s)", "InvalidEditSpec"
        ) from None


def check_font(spec: EditSpec, font_path: str) -> None:
    """Refuses, for good, a spec laid out with another font than `font_path`. Lines were
    measured with one file; drawing them with another would overflow boxes or leave gaps,
    and the names alone would not show it."""
    name = Path(font_path).name
    if name != spec.font:
        raise _refuse(
            f"the spec was laid out with {spec.font}, this renderer has {name}", "FontMismatch"
        )
    if hashlib.sha256(Path(font_path).read_bytes()).hexdigest() != spec.font_sha256:
        raise _refuse(
            f"{name} here is not the file the spec was laid out with (the hashes differ)",
            "FontMismatch",
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _filter_graph(spec: EditSpec, *, captions_at: tuple[int, int] | None) -> str:
    w, h, fps = spec.width, spec.height, spec.fps
    seconds = spec.duration_ms / 1000
    # object-fit: cover, at the spec's size and rate; black once the source has ended.
    video = (
        f"[0:v]setpts=PTS-STARTPTS,fps={fps},"
        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
        f"tpad=stop_mode=add:stop_duration={seconds:.3f}:color=black"
    )
    if spec.cover is not None:
        r = spec.cover.region
        video += (
            f",drawbox=x={r.x}:y={r.y}:w={r.w}:h={r.h}:"
            f"color=0x{spec.cover.color[1:]}@{spec.cover.opacity_pct / 100:.2f}:t=fill"
        )
    if captions_at is None:
        video += "[v]"
    else:
        x, y = captions_at
        video += f"[base];[base][2:v]overlay=x={x}:y={y}:eof_action=pass:format=auto[v]"
    # The dubbed track is the only sound, as long as the picture.
    return f"{video};[1:a]apad[a]"


def ffmpeg_argv(
    spec: EditSpec, *, source: Path, audio: Path, captions: tuple[Path, int, int] | None, out: Path
) -> list[str]:
    frames = frame_count(spec.duration_ms, spec.fps)
    inputs = ["-i", str(source), "-i", str(audio)]
    if captions is not None:
        inputs += ["-f", "concat", "-i", str(captions[0])]
    graph = _filter_graph(spec, captions_at=None if captions is None else captions[1:])
    return [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-v", "error",
        *inputs,
        "-filter_complex", graph,
        "-map", "[v]", "-map", "[a]",
        "-frames:v", str(frames), "-t", f"{frames / spec.fps:.6f}", "-r", str(spec.fps),
        *VIDEO_CODEC, *AUDIO_CODEC,
        "-movflags", "+faststart",
        str(out),
    ]  # fmt: skip


class _Deadline(Exception):
    pass


def _check(cancel: threading.Event | None, deadline_at: float | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled
    if deadline_at is not None and time.monotonic() >= deadline_at:
        raise _Deadline


def _encode(
    argv: list[str], log_file: Path, cancel: threading.Event | None, deadline_at: float | None
) -> None:
    """Runs ffmpeg in its own process group and kills the group if the render is
    cancelled or runs out of time."""
    with log_file.open("wb") as stderr:
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=_child_env(),
                start_new_session=True,
            )
        except OSError:
            raise PermanentProviderError("ffmpeg: the binary could not be started") from None
        try:
            while True:
                try:
                    returncode = process.wait(timeout=_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    _check(cancel, deadline_at)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    if returncode < 0:
        # Killed from outside (the OOM killer, say): another attempt may get through.
        raise TransientProviderError(f"ffmpeg: killed by signal {-returncode}")
    if returncode != 0:
        tail = log_file.read_text(errors="replace").strip().splitlines()[-3:]
        raise PermanentProviderError("ffmpeg: the render failed: " + " | ".join(tail)[:300])


def render_file(
    spec: EditSpec,
    *,
    source: Path,
    audio: Path,
    font_path: str,
    out: Path,
    work_dir: Path,
    cancel: threading.Event | None = None,
    deadline_at: float | None = None,
    meter: FontMeter | None = None,
) -> None:
    """Draws `spec` from local files into `out`. Raises `Cancelled` when `cancel` is set
    and `_Deadline` past `deadline_at`; ffmpeg is never left running."""
    painter = CaptionPainter(font_path, meter)
    track = caption_track(
        spec.captions,
        spec.caption_box,
        fps=spec.fps,
        frames=frame_count(spec.duration_ms, spec.fps),
        painter=painter,
        work_dir=work_dir,
        cancel=cancel,
    )
    _check(cancel, deadline_at)
    argv = ffmpeg_argv(
        spec,
        source=source,
        audio=audio,
        captions=None if track is None else (track.playlist, track.x, track.y),
        out=out,
    )
    _encode(argv, work_dir / "ffmpeg.log", cancel, deadline_at)


class RenderActivity:
    def __init__(
        self,
        store: ObjectStore,
        *,
        font_path: str,
        tmp_root: Path | None = None,
        deadline_s: float | None = None,
        heartbeat_every: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._store = store
        self._font_path = font_path
        self._tmp_root = tmp_root
        self._deadline_s = deadline_s
        self._heartbeat_every = heartbeat_every
        self._meter = FontMeter(font_path)

    @activity.defn(name="render")
    async def render(self, input: RenderInput) -> RenderResult:
        tag_session(input.aweme_id)
        if not _under_video(input.spec_key, input.aweme_id):
            raise _refuse("spec_key is not under this video's work/ folder", "InvalidInput")
        spec = read_spec(
            await run_blocking(
                partial(self._store.get_bytes, input.spec_key, produced_by="captions")
            )
        )
        for key in (spec.source_key, spec.audio_key):
            if not _under_video(key, input.aweme_id):
                raise _refuse(f"{key} is not under this video's work/ folder", "InvalidEditSpec")
        check_font(spec, self._font_path)

        sha = spec_sha256(spec)
        key = render_key(input.aweme_id, sha)
        span = trace.get_current_span()
        span.set_attribute("render.spec_sha256", sha)
        if await run_blocking(self._store.exists, key):
            span.set_attribute("render.reused", True)
            return RenderResult(render_key=key, spec_sha256=sha)

        started = time.monotonic()
        deadline_at = started + (
            self._deadline_s
            if self._deadline_s is not None
            else render_deadline_s(spec.duration_ms)
        )
        work = Path(tempfile.mkdtemp(prefix="render-", dir=self._tmp_root))
        timings: dict[str, float] = {}
        try:
            with provider_errors():
                source, audio, out = work / "source.mp4", work / "audio.wav", work / "render.mp4"
                await run_blocking(
                    partial(
                        self._store.download_file, spec.source_key, source, produced_by="captions"
                    )
                )
                await run_blocking(
                    partial(
                        self._store.download_file,
                        spec.audio_key,
                        audio,
                        produced_by="assemble_audio",
                    )
                )
                if await run_blocking(_sha256_file, audio) != spec.audio_sha256:
                    raise _refuse(
                        "the audio is not the track this spec was made for", "AudioHashMismatch"
                    )
                timings["fetch_s"] = time.monotonic() - started
                await self._draw(spec, source, audio, out, work, deadline_at)
                timings["draw_s"] = time.monotonic() - started - timings["fetch_s"]
                await run_blocking(
                    partial(self._store.put_file, key, out, content_type="video/mp4")
                )
                timings["total_s"] = time.monotonic() - started
        finally:
            shutil.rmtree(work, ignore_errors=True)
        log.info(
            "render %s: %s",
            sha[:12],
            ", ".join(f"{name}={value:.1f}" for name, value in timings.items()),
        )
        span.set_attribute("render.draw_s", round(timings["draw_s"], 3))
        return RenderResult(render_key=key, spec_sha256=sha)

    async def _draw(
        self, spec: EditSpec, source: Path, audio: Path, out: Path, work: Path, deadline_at: float
    ) -> None:
        """Draws in a thread, heartbeating while it runs. On cancellation the thread is
        told to stop (it kills ffmpeg) and is waited for, so nothing is still writing in
        the working directory when it is removed."""
        cancel = threading.Event()
        task = asyncio.ensure_future(
            asyncio.to_thread(
                render_file,
                spec,
                source=source,
                audio=audio,
                font_path=self._font_path,
                out=out,
                work_dir=work,
                cancel=cancel,
                deadline_at=deadline_at,
                meter=self._meter,
            )
        )
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self._heartbeat_every)
                if done:
                    break
                activity.heartbeat()
        except asyncio.CancelledError:
            cancel.set()
            await asyncio.wait({task})
            if not task.cancelled():
                task.exception()  # retrieved: it is the cancellation's, not a failure
            raise
        try:
            task.result()
        except _Deadline:
            raise _refuse(
                "the render did not finish within its deadline", "RenderDeadline"
            ) from None
