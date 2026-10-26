import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from localization.providers.errors import PermanentProviderError, TransientProviderError

# What demucs needs to run and find its cached weights; nothing else is passed on,
# so a credential in the worker's environment never reaches the subprocess.
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TORCH_HOME", "XDG_CACHE_HOME")


def _child_env(work_dir: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env["TMPDIR"] = str(work_dir)
    return env


# Seconds of separation allowed per second of media.
TIMEOUT_PER_MEDIA_SECOND = 2.5


class _Cancelled(Exception):
    pass


class DemucsSeparator:
    """Splits vocals from background with the `demucs` CLI.

    The command is an argv list (no shell) with `--` before the input path, so a
    path starting with `-` is a file, not a flag. The output directory comes from
    `tempfile.mkdtemp()`, never from an id.
    """

    def __init__(self, binary: str = "demucs", *, timeout_seconds: float = 600.0) -> None:
        self._binary = binary
        self._timeout = timeout_seconds

    def timeout_for(self, duration_ms: int) -> float:
        """CPU separation takes about as long as the media; allow well over that, but
        never less than the configured floor."""
        return max(self._timeout, TIMEOUT_PER_MEDIA_SECOND * duration_ms / 1000)

    def separate(
        self, input_path: Path, duration_ms: int = 0, cancel: threading.Event | None = None
    ) -> tuple[Path, Path, Path]:
        """Returns `(work_dir, vocals, background)`. The caller removes `work_dir`.

        `cancel`, when set, kills the demucs process: the activity that started it
        was cancelled, and an orphaned separation would hold a CPU slot."""
        work_dir = Path(tempfile.mkdtemp(prefix="demucs-"))
        argv = [
            self._binary,
            "--two-stems=vocals",
            "-o",
            str(work_dir),
            "--",
            str(input_path),
        ]
        try:
            returncode = self._run(argv, work_dir, self.timeout_for(duration_ms), cancel)
        except subprocess.TimeoutExpired:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise TransientProviderError("demucs: timeout") from None
        except _Cancelled:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise TransientProviderError("demucs: cancelled") from None
        except OSError:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise PermanentProviderError("demucs: binary could not be started") from None
        if returncode != 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise PermanentProviderError(f"demucs: exited with status {returncode}")
        vocals = next(work_dir.rglob("vocals.*"), None)
        background = next(work_dir.rglob("no_vocals.*"), None)
        if vocals is None or background is None:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise PermanentProviderError("demucs: expected stems were not produced")
        return work_dir, vocals, background

    @staticmethod
    def _run(
        argv: list[str], work_dir: Path, timeout: float, cancel: threading.Event | None
    ) -> int:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_child_env(work_dir),
            start_new_session=True,
        )
        waited = 0.0
        try:
            while True:
                try:
                    return process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    waited += 0.2
                if cancel is not None and cancel.is_set():
                    raise _Cancelled
                if waited >= timeout:
                    raise subprocess.TimeoutExpired(argv, timeout)
        finally:
            if process.poll() is None:
                # The whole group: demucs spawns ffmpeg and workers of its own.
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
