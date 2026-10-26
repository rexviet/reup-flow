"""Demucs process control: its timeout follows the media, and a cancelled activity
does not leave it running."""

import asyncio
import os
import stat
import time
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from localization.activities.ffmpeg import run_blocking
from localization.providers.demucs import DemucsSeparator
from localization.providers.errors import TransientProviderError


def _sleeper(tmp_path: Path, seconds: float) -> str:
    """A stand-in `demucs` that writes its pid, sleeps, then writes both stems."""
    script = tmp_path / "fake-demucs"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo $$ > "{tmp_path}/pid"\n'
        # The sleep is a grandchild of the process the separator starts.
        f"sleep {seconds} &\n"
        f'echo $! > "{tmp_path}/grandchild"\n'
        "wait\n"
        'out="$3"\nmkdir -p "$out/htdemucs/in"\n'
        'echo v > "$out/htdemucs/in/vocals.wav"\necho b > "$out/htdemucs/in/no_vocals.wav"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_the_timeout_grows_with_the_media_but_never_below_the_floor() -> None:
    sep = DemucsSeparator("demucs", timeout_seconds=600)
    assert sep.timeout_for(0) == 600
    assert sep.timeout_for(60_000) == 600
    assert sep.timeout_for(1_200_000) == 3000  # 20 minutes of media: 2.5x


def test_a_long_video_is_given_the_time_its_length_needs(tmp_path: Path) -> None:
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"x")
    binary = _sleeper(tmp_path, 1.5)
    # 1.5 s of work against a 0.4 s floor: only the duration-scaled allowance
    # (2.5 x 10 s) lets it finish.
    work_dir, vocals, _ = DemucsSeparator(binary, timeout_seconds=0.4).separate(
        audio, duration_ms=10_000
    )
    assert vocals.exists()
    with pytest.raises(TransientProviderError, match="timeout"):
        DemucsSeparator(binary, timeout_seconds=0.4).separate(audio, duration_ms=0)


async def test_cancelling_the_activity_kills_the_demucs_process(tmp_path: Path) -> None:
    import threading

    audio = tmp_path / "in.wav"
    audio.write_bytes(b"x")
    sep = DemucsSeparator(_sleeper(tmp_path, 60), timeout_seconds=600)
    cancel = threading.Event()

    async def work() -> None:
        await run_blocking(sep.separate, audio, 0, cancel, cancel=cancel, heartbeat_every=0.05)

    task = asyncio.ensure_future(ActivityEnvironment().run(work))
    pid_file = tmp_path / "pid"
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    grandchild_file = tmp_path / "grandchild"
    for _ in range(100):
        if grandchild_file.exists() and grandchild_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    grandchild = int(grandchild_file.read_text())
    os.kill(pid, 0)  # it is running
    os.kill(grandchild, 0)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for victim, what in ((pid, "the demucs process"), (grandchild, "its child process")):
            gone = False
            for _ in range(60):
                try:
                    os.kill(victim, 0)
                except ProcessLookupError:
                    gone = True
                    break
                time.sleep(0.1)
            assert gone, f"{what} outlived the cancelled activity"
    finally:
        cancel.set()
        for leftover in (pid, grandchild):
            try:
                os.kill(leftover, 9)
            except ProcessLookupError:
                pass
