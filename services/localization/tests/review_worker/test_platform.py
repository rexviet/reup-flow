"""The Windows implementation (exercised through mocks on any OS), the platform factory, worker
roles and the optional GPU flag."""

import dataclasses
import signal
import subprocess
import sys
import threading

import pytest

from localization.config import ConfigError
from localization.platform import NotSupported, get_platform
from localization.platform.posix import PosixProcessControl
from localization.platform.windows import (
    CREATE_NEW_PROCESS_GROUP,
    WindowsFileLock,
    WindowsProcessControl,
)
from localization.review_bridge.repository import Repository
from localization.review_worker import final_render, main
from localization.review_worker.config import WorkerConfig, parse_flag, parse_roles

# -- factory -------------------------------------------------------------------


@pytest.mark.parametrize(("os_name", "expected"), [("posix", "posix"), ("nt", "windows")])
def test_auto_follows_the_operating_system(os_name, expected):
    assert get_platform({}, os_name=os_name).name == expected
    assert get_platform({"REVIEW_PLATFORM": "auto"}, os_name=os_name).name == expected
    assert get_platform({"REVIEW_PLATFORM": " "}, os_name=os_name).name == expected


def test_explicit_native_choice_is_accepted():
    assert get_platform({"REVIEW_PLATFORM": "POSIX"}, os_name="posix").name == "posix"
    assert get_platform({"REVIEW_PLATFORM": "windows"}, os_name="nt").name == "windows"


def test_unknown_value_stops_instead_of_falling_back():
    with pytest.raises(ConfigError, match="REVIEW_PLATFORM must be one of"):
        get_platform({"REVIEW_PLATFORM": "macos"}, os_name="posix")


def test_the_other_os_needs_force():
    with pytest.raises(ConfigError, match="REVIEW_PLATFORM_FORCE"):
        get_platform({"REVIEW_PLATFORM": "windows"}, os_name="posix")
    with pytest.raises(ConfigError, match="REVIEW_PLATFORM_FORCE"):
        get_platform({"REVIEW_PLATFORM": "posix"}, os_name="nt")
    forced = get_platform({"REVIEW_PLATFORM": "windows", "REVIEW_PLATFORM_FORCE": "1"})
    assert forced.name == "windows"
    assert isinstance(forced.process, WindowsProcessControl)


def test_repository_imports_without_fcntl():
    """Windows has no `fcntl`; the module must not load it at import time."""
    code = (
        "import sys; sys.modules['fcntl'] = None\n"
        "import localization.review_bridge.repository, localization.review_worker.main\n"
        "print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


# -- Windows implementation ------------------------------------------------------


def test_windows_file_lock_is_refused(tmp_path):
    repo = Repository(None, tmp_path / "review", file_lock=WindowsFileLock())
    with pytest.raises(NotSupported), repo.lock("7001", 1):
        pass


class FakeProc:
    pid = 4321

    def __init__(self):
        self.waits = []

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return 0


def test_windows_spawn_uses_a_new_process_group(tmp_path, monkeypatch):
    seen = {}

    def popen(command, **kwargs):
        seen.update(kwargs, command=command)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", popen)
    with (tmp_path / "log").open("wb") as out:
        WindowsProcessControl({}).spawn(["node", "x"], cwd=tmp_path, env={"A": "1"}, stdout=out)
    assert seen["creationflags"] == CREATE_NEW_PROCESS_GROUP == 0x200
    assert "start_new_session" not in seen
    assert seen["stdin"] is subprocess.DEVNULL and seen["stderr"] is subprocess.STDOUT
    assert seen["env"] == {"A": "1"} and seen["cwd"] == tmp_path


def test_windows_terminate_kills_the_whole_tree():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))

    proc = FakeProc()
    WindowsProcessControl({}, run=run).terminate_tree(proc)
    assert [c for c, _ in calls] == [["taskkill", "/PID", "4321", "/T", "/F"]]
    assert calls[0][1]["check"] is False and calls[0][1]["timeout"] == 30
    assert proc.waits == [5]


def test_windows_terminate_survives_a_missing_taskkill():
    def run(command, **kwargs):
        raise FileNotFoundError("taskkill")

    proc = FakeProc()
    WindowsProcessControl({}, run=run).terminate_tree(proc)
    assert proc.waits == [5]


def test_windows_child_env_has_system_variables_and_no_credentials(tmp_path):
    environ = {
        "PATH": r"C:\Windows;C:\nodejs", "SystemRoot": r"C:\Windows",
        "USERPROFILE": r"C:\Users\me", "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
        "APPDATA": r"C:\Users\me\AppData\Roaming", "TEMP": r"C:\old", "TMP": r"C:\old",
        "LOC_DATABASE_URL": "postgresql://secret", "LOC_S3_SECRET_ACCESS_KEY": "secret",
        "OPENROUTER_API_KEY": "secret",
    }  # fmt: skip
    env = WindowsProcessControl(environ).child_env(tmp_path)
    assert env["SystemRoot"] == r"C:\Windows" and env["USERPROFILE"] == r"C:\Users\me"
    assert env["LOCALAPPDATA"] and env["APPDATA"] and env["PATH"].endswith("nodejs")
    assert env["TEMP"] == env["TMP"] == str(tmp_path)
    assert not {"LOC_DATABASE_URL", "LOC_S3_SECRET_ACCESS_KEY", "OPENROUTER_API_KEY"} & set(env)


def test_run_cli_uses_the_given_process_control(tmp_path):
    used = []

    class Recording(PosixProcessControl):
        def spawn(self, command, **kwargs):
            used.append("spawn")
            return super().spawn(command, **kwargs)

        def terminate_tree(self, proc, grace_s=10.0):
            used.append("terminate")
            super().terminate_tree(proc, grace_s)

    final_render.run_cli(
        [sys.executable, "-c", "pass"], cwd=tmp_path, env={}, log_path=tmp_path / "log",
        timeout_s=30, renew_every_s=3600, renew=lambda: True, cancelled=threading.Event(),
        process=Recording(),
    )  # fmt: skip
    assert used == ["spawn", "terminate"]


# -- worker settings ----------------------------------------------------------------


def test_roles():
    everything = {"render", "studio"}
    assert parse_roles(None) == parse_roles("") == parse_roles("all") == everything
    assert parse_roles("render") == {"render"}
    assert parse_roles(" Studio ") == {"studio"}
    assert parse_roles("render,studio") == everything
    for bad in ("gpu", "render,all", ","):
        with pytest.raises(ConfigError):
            parse_roles(bad)


def test_flag():
    assert parse_flag("X", None) is False and parse_flag("X", "0") is False
    assert parse_flag("X", "true") is True and parse_flag("X", "1") is True
    with pytest.raises(ConfigError):
        parse_flag("X", "maybe")


def env_for(tmp_path, **extra):
    return {"REVIEW_WORKER_DATA_ROOT": str(tmp_path / "data" / "review"), **extra}


def test_defaults_keep_the_mac_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("LOC_DATABASE_URL", "postgresql://localization@db/reup")
    cfg = WorkerConfig.from_environ(env_for(tmp_path))
    assert cfg.roles == {"render", "studio"} and cfg.name == "review-worker"
    assert cfg.render_gpu is False


def test_windows_render_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LOC_DATABASE_URL", "postgresql://localization@db/reup")
    cfg = WorkerConfig.from_environ(
        env_for(
            tmp_path,
            REVIEW_WORKER_ROLES="render",
            REVIEW_WORKER_NAME="render-worker-win",
            FINAL_RENDER_GPU="true",
        )  # fmt: skip
    )
    assert cfg.roles == {"render"} and cfg.name == "render-worker-win" and cfg.render_gpu
    for bad in ("", "Render Worker", "-x", "a" * 64):
        with pytest.raises(ConfigError, match="REVIEW_WORKER_NAME"):
            WorkerConfig.from_environ(env_for(tmp_path, REVIEW_WORKER_NAME=bad))


def test_render_command_is_unchanged_without_the_gpu_flag(tmp_path):
    cfg = WorkerConfig(
        database_url="x", data_root=tmp_path, work_root=tmp_path, node="node",
        hyperframes_bin="/h/hyperframes.mjs", render_workers=4,
    )  # fmt: skip
    folder, out = tmp_path / "project", tmp_path / "final.mp4"
    assert final_render.render_command(cfg, folder, out) == [
        "node", "/h/hyperframes.mjs", "render", str(folder),
        "-o", str(out), "-q", "delivery", "--workers", "4",
    ]  # fmt: skip
    gpu = final_render.render_command(dataclasses.replace(cfg, render_gpu=True), folder, out)
    assert gpu[-1] == "--gpu" and gpu[:-1] == final_render.render_command(cfg, folder, out)


def test_stop_signals(monkeypatch):
    monkeypatch.delattr(signal, "SIGBREAK", raising=False)
    assert main.stop_signals() == (signal.SIGTERM, signal.SIGINT)
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    assert main.stop_signals() == (signal.SIGTERM, signal.SIGINT, 21)
