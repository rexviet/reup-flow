"""The Windows render PC imports the same worker as the Mac. A module that touches a
POSIX-only `os` name at import time stops that worker before it can say why (it happened:
`os.O_DIRECTORY` in folder exports), so the import is checked here with those names gone."""

import subprocess
import sys
import threading
from pathlib import Path

from localization.review_worker import folder_exports

POSIX_ONLY = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "getuid", "geteuid", "killpg", "setsid")

SCRIPT = f"""
import os
for name in {POSIX_ONLY!r}:
    if hasattr(os, name):
        delattr(os, name)
import localization.review_worker.main
from localization.review_worker import folder_exports
assert folder_exports.SUPPORTED is False
print("ok")
"""


def test_the_worker_imports_without_posix_only_os_names() -> None:
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().endswith("ok")


def test_folder_exports_refuse_where_o_nofollow_is_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(folder_exports, "SUPPORTED", False)

    class NoDb:
        def execute(self, *_: object, **__: object) -> None:
            raise AssertionError("nothing may be claimed without O_NOFOLLOW")

    handled = folder_exports.drain(
        NoDb(), store=None, root=tmp_path, min_free_gb=0, stopping=threading.Event()
    )
    assert handled == 0
