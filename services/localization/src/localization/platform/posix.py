"""macOS and Linux: `flock` and process groups. Shared by both; macOS differs only in how a
group holding a zombie answers, which `group_alive` already covers."""

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import IO


class PosixFileLock:
    @contextlib.contextmanager
    def hold(self, path: Path):
        import fcntl  # POSIX only: importing it at module level would break Windows

        with path.open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield


def group_alive(pgid: int, sig: int = 0) -> bool:
    """Signal the group; False once nothing is left. macOS answers EPERM, not ESRCH, for a
    group that holds only a zombie, so both mean gone."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class PosixProcessControl:
    def spawn(
        self, command: list[str], *, cwd: Path, env: dict[str, str], stdout: IO[bytes]
    ) -> subprocess.Popen:
        return subprocess.Popen(
            command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )  # fmt: skip

    def terminate_tree(self, proc: subprocess.Popen, grace_s: float = 10.0) -> None:
        """SIGTERM to the group, up to `grace_s` for it to go, then SIGKILL."""
        proc.poll()  # reap an exited leader so the group is not held by a zombie
        if group_alive(proc.pid, signal.SIGTERM):
            for _ in range(round(grace_s / 0.1)):
                if not group_alive(proc.pid):
                    break
                time.sleep(0.1)
            else:
                group_alive(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)

    def child_env(self, tmp: Path) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(tmp)}
        env["HOME"] = os.environ.get("HOME", "")
        env["LANG"] = os.environ.get("LANG", "en_US.UTF-8")
        return env
