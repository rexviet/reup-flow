"""The only things the review worker does differently per operating system: holding a
file lock, and starting, stopping and isolating the renderer's process tree."""

import subprocess
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol


class NotSupported(RuntimeError):
    """The operation has no implementation on this operating system."""


class FileLock(Protocol):
    def hold(self, path: Path) -> AbstractContextManager[None]:
        """An exclusive lock on `path` (created if missing) for the duration of the block."""
        ...


class ProcessControl(Protocol):
    def spawn(
        self, command: list[str], *, cwd: Path, env: dict[str, str], stdout: IO[bytes]
    ) -> subprocess.Popen:
        """Start `command` in a group of its own, stderr merged into `stdout`, no stdin."""
        ...

    def terminate_tree(self, proc: subprocess.Popen, grace_s: float = 10.0) -> None:
        """Stop `proc` and everything it started, also when `proc` itself already exited."""
        ...

    def child_env(self, tmp: Path) -> dict[str, str]:
        """The renderer's whole environment: no database or storage credentials."""
        ...


@dataclass(frozen=True)
class Platform:
    name: str
    file_lock: FileLock
    process: ProcessControl
