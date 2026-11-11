#!/usr/bin/env python3
"""Install this checkout's review watcher in the current macOS login session."""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = "com.reup-flow.review-auto-open"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "stop"], nargs="?", default="install")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("automatic Studio opening requires a logged-in macOS desktop")
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    loaded = subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
    if loaded:
        subprocess.run(["launchctl", "bootout", target], check=True)
    if args.action == "stop":
        plist.unlink(missing_ok=True)
        print("Review auto-open stopped; queued state retained")
        return
    node, docker = shutil.which("node"), shutil.which("docker")
    if not node or not docker:
        parser.error("Node 22.13+ and Docker must be on PATH")
    runtime = ROOT / "data/review-launcher"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    plist.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": LABEL,
        "ProgramArguments": [str(Path(node).resolve()), str(ROOT / "scripts/review-auto-open.mjs")],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": os.pathsep.join(dict.fromkeys([
                str(Path(node).parent), str(Path(docker).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"
            ])),
        },
        "StandardOutPath": str(runtime / "auto-open.log"),
        "StandardErrorPath": str(runtime / "auto-open-error.log"),
    }
    plist.write_bytes(plistlib.dumps(config))
    plist.chmod(0o600)
    # bootout returns before launchd has finished removing the old job.
    for attempt in range(30):
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist)], capture_output=True
        )
        if result.returncode == 0:
            break
        if result.returncode != 5 or attempt == 29:
            raise RuntimeError(f"launchctl bootstrap failed with exit {result.returncode}")
        time.sleep(0.25)
    print(f"Installed {LABEL}; logs and persistent state: {runtime}")


if __name__ == "__main__":
    main()
