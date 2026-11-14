"""Native Windows, for a machine that only renders: no `fcntl`, no process groups that
signals reach, and a renderer (Node, Chrome) that needs the Windows system variables."""

import contextlib
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from localization.platform.base import NotSupported

# Not defined by `subprocess` outside Windows; the value is the Win32 constant.
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

# What Node and Chrome need to start and to reach the network on Windows. None of these hold
# a credential; the worker's own database and storage settings are deliberately absent.
CHILD_ENV_KEYS = (
    "PATH", "PATHEXT", "SystemRoot", "SystemDrive", "windir", "ComSpec",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
    "LOCALAPPDATA", "APPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)",
    "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG",
)  # fmt: skip


class WindowsFileLock:
    def hold(self, path: Path):
        raise NotSupported(
            "project locks are taken only by the review bridge, which does not run on Windows"
        )


class WindowsProcessControl:
    def __init__(self, environ: Mapping[str, str] | None = None, run=subprocess.run):
        self.environ = os.environ if environ is None else environ
        self.run = run

    def spawn(
        self, command: list[str], *, cwd: Path, env: dict[str, str], stdout: IO[bytes]
    ) -> subprocess.Popen:
        # A group of its own: Ctrl+C in the worker's console does not reach the renderer, so
        # the worker decides when it stops and can give the row back first.
        return subprocess.Popen(
            command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=CREATE_NEW_PROCESS_GROUP,
        )  # fmt: skip

    def terminate_tree(self, proc: subprocess.Popen, grace_s: float = 10.0) -> None:
        """`taskkill /T /F` on the renderer's tree. Console programs get no graceful signal
        from outside, so `grace_s` is not used. While `proc` is referenced its handle is open
        and Windows cannot hand its PID to another process. Known limit: `/T` follows parent
        links, so children of a renderer that already exited are not found."""
        proc.poll()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=30,
            )  # fmt: skip
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)

    def child_env(self, tmp: Path) -> dict[str, str]:
        env = {key: self.environ[key] for key in CHILD_ENV_KEYS if key in self.environ}
        env["TEMP"] = env["TMP"] = str(tmp)
        return env
