"""The POSIX behaviour the renderer relies on on the Mac, pinned through the public names so
that it cannot change while the OS-specific parts move behind an interface."""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from localization.review_bridge.repository import Repository
from localization.review_worker import final_render

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX behaviour")


def test_render_env_carries_only_what_the_cli_needs(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/x/bin")
    monkeypatch.setenv("HOME", "/home/me")
    monkeypatch.setenv("LANG", "vi_VN.UTF-8")
    monkeypatch.setenv("LOC_DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("LOC_S3_SECRET_ACCESS_KEY", "secret")
    env = final_render.render_env(tmp_path)
    assert env == {
        "PATH": "/x/bin", "TMPDIR": str(tmp_path), "HOME": "/home/me", "LANG": "vi_VN.UTF-8",
    }  # fmt: skip


def test_render_env_defaults(tmp_path, monkeypatch):
    for key in ("PATH", "HOME", "LANG"):
        monkeypatch.delenv(key, raising=False)
    env = final_render.render_env(tmp_path)
    assert env == {
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
        "HOME": "",
        "LANG": "en_US.UTF-8",
    }


class FakeProc:
    pid = 424242

    def __init__(self):
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return 0

    def poll(self):
        return 0


def test_renderer_starts_in_its_own_session_with_stderr_merged(tmp_path, monkeypatch):
    seen = {}

    def popen(command, **kwargs):
        seen.update(kwargs, command=command)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
    final_render.run_cli(
        ["renderer"], cwd=tmp_path, env={"PATH": "/bin"}, log_path=tmp_path / "log",
        timeout_s=30, renew_every_s=3600, renew=lambda: True, cancelled=threading.Event(),
    )  # fmt: skip
    assert seen["command"] == ["renderer"]
    assert seen["start_new_session"] is True
    assert seen["stdin"] is subprocess.DEVNULL and seen["stderr"] is subprocess.STDOUT
    assert seen["cwd"] == tmp_path and seen["env"] == {"PATH": "/bin"}


def test_kill_group_sends_sigterm_then_sigkill_after_ten_seconds(monkeypatch):
    sent, slept = [], []

    def killpg(pgid, sig):
        sent.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr("time.sleep", slept.append)
    proc = FakeProc()
    final_render.kill_group(proc)
    signals = [sig for _, sig in sent]
    assert signals[0] == signal.SIGTERM and signals[-1] == signal.SIGKILL
    assert signals[1:-1] == [0] * 100 and all(pgid == proc.pid for pgid, _ in sent)
    assert slept == [0.1] * 100
    assert proc.waits[-1] == 5


def test_kill_group_stops_waiting_once_the_group_is_gone(monkeypatch):
    sent = []

    def killpg(pgid, sig):
        sent.append(sig)
        if sig == 0 and len(sent) > 3:
            raise PermissionError()  # macOS: a group holding only a zombie

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr("time.sleep", lambda _: None)
    final_render.kill_group(FakeProc())
    assert sent == [signal.SIGTERM, 0, 0, 0] and signal.SIGKILL not in sent


def test_group_alive_treats_esrch_and_eperm_as_gone(monkeypatch):
    for error in (ProcessLookupError, PermissionError):
        monkeypatch.setattr(os, "killpg", lambda *_, e=error: (_ for _ in ()).throw(e()))
        assert final_render.group_alive(1) is False
    monkeypatch.setattr(os, "killpg", lambda *_: None)
    assert final_render.group_alive(1) is True


TRY_LOCK = (
    "import fcntl,sys;f=open(sys.argv[1],'a+b')\n"
    "try:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print('free')\n"
    "except BlockingIOError:\n print('held')"
)


def try_lock(path: Path) -> str:
    return subprocess.run(
        [sys.executable, "-c", TRY_LOCK, str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_project_lock_blocks_another_process(tmp_path):
    repo = Repository(None, tmp_path / "review")
    with repo.lock("7001", 1) as folder:
        assert folder == tmp_path / "review" / "7001" / "g1"
        assert try_lock(folder / ".companion.lock") == "held"
    assert try_lock(folder / ".companion.lock") == "free"
