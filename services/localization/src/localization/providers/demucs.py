import shutil
import subprocess
import tempfile
from pathlib import Path

from localization.providers.errors import PermanentProviderError, TransientProviderError


class DemucsSeparator:
    """Splits vocals from background with the `demucs` CLI.

    The command is an argv list (no shell) with `--` before the input path, so a
    path starting with `-` is a file, not a flag. The output directory comes from
    `tempfile.mkdtemp()`, never from an id.
    """

    def __init__(self, binary: str = "demucs", *, timeout_seconds: float = 600.0) -> None:
        self._binary = binary
        self._timeout = timeout_seconds

    def separate(self, input_path: Path) -> tuple[Path, Path, Path]:
        """Returns `(work_dir, vocals, background)`. The caller removes `work_dir`."""
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
            result = subprocess.run(argv, capture_output=True, timeout=self._timeout, check=False)
        except subprocess.TimeoutExpired:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise TransientProviderError("demucs: timeout") from None
        except OSError:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise PermanentProviderError("demucs: binary could not be started") from None
        if result.returncode != 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise PermanentProviderError(f"demucs: exited with status {result.returncode}")
        vocals = next(work_dir.rglob("vocals.*"), None)
        background = next(work_dir.rglob("no_vocals.*"), None)
        if vocals is None or background is None:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise PermanentProviderError("demucs: expected stems were not produced")
        return work_dir, vocals, background
