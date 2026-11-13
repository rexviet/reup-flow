#!/usr/bin/env python3
"""Start VoiceStudio the way the localization pipeline needs it (macOS, Apple Silicon).

  * Speech recognition runs on MLX Whisper (the Apple GPU), not on CPU faster-whisper.
  * MLX's wav2vec2 word alignment is off (OMNIVOICE_MLX_FORCED_ALIGN=0): its tight word times
    expose pauses inside sung phrases and the pipeline then splits one sung line in pieces.
  * The app starts through OMNIVOICE_BACKEND_CMD, so it does not refresh its managed runtime
    from the app bundle and drop the patch in scripts/voicestudio-patches/.

The patch is applied only to a file that is byte-identical to the one in the app bundle
(known by checksum). Anything else is left alone and reported.

  scripts/launch-voicestudio.py            patch if needed, then open the app
  scripts/launch-voicestudio.py --check    report the state of the runtime, change nothing
  scripts/launch-voicestudio.py --reset    put the patched files back to the app's own version

Quit VoiceStudio first when it has to pick up a change: a running app is left as it is.
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent / "voicestudio-patches"
APP = Path("/Applications/VoiceStudio.app")
BUNDLE = APP / "Contents/Resources"
RUNTIME = Path.home() / "Library/Application Support/VoiceStudio/runtime/project"
PORT = 3900
MODEL_GLOB = str(Path.home() / ".cache/huggingface/hub/models--mlx-community--whisper-large-v3-mlx/snapshots/*/weights.npz")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state(name: str, entry: dict) -> str:
    target = RUNTIME / name
    if not target.exists():
        return "missing"
    digest = sha(target)
    return "patched" if digest == entry["patched"] else "original" if digest == entry["original"] else "changed"


def listening() -> bool:
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report the runtime state and change nothing")
    parser.add_argument("--reset", action="store_true", help="restore the patched files from the app bundle")
    args = parser.parse_args()
    if sys.platform != "darwin" or not APP.exists():
        sys.exit("VoiceStudio.app was not found in /Applications (macOS only).")
    manifest = json.loads((HERE / "manifest.json").read_text())
    states = {name: state(name, entry) for name, entry in manifest.items()}
    for name, current in states.items():
        print(f"{name}: {current}")

    if args.check:
        return 0 if all(v in ("patched", "original") for v in states.values()) else 1
    if listening():
        print(f"VoiceStudio already answers on port {PORT}; quit it first to apply a change.")
        return 0

    for name, entry in manifest.items():
        source = BUNDLE / name
        if args.reset or states[name] == "changed":
            if sha(source) != entry["original"]:
                sys.exit(f"{name}: the app bundle's file is not the version this patch was made for "
                         "(VoiceStudio was updated?). Review scripts/voicestudio-patches before going on.")
            if not args.reset:
                sys.exit(f"{name}: differs from both known versions. Run with --reset to restore "
                         "the app's own file, then run again.")
            shutil.copy2(source, RUNTIME / name)
            states[name] = "original"
            print(f"{name}: restored from the app bundle")
        if args.reset:
            continue
        if states[name] == "missing":
            sys.exit(f"{name}: not in the runtime ({RUNTIME}). Open VoiceStudio once, then run again.")
        if states[name] == "original":
            subprocess.run(["patch", "-p1", "--input", str(HERE / entry["patch"])],
                           cwd=RUNTIME, check=True, stdout=subprocess.DEVNULL)
            if sha(RUNTIME / name) != entry["patched"]:
                sys.exit(f"{name}: the patch did not give the expected file.")
            print(f"{name}: patched")
    if args.reset:
        return 0

    if not glob.glob(MODEL_GLOB):
        print("note: mlx-community/whisper-large-v3-mlx (2.9 GB) is not downloaded; the first "
              "transcription downloads it and may time out. Fetch it first, e.g. with huggingface_hub.")
    command = [str(RUNTIME / ".venv/bin/python"), "-m", "uvicorn", "main:app", "--app-dir",
               str(RUNTIME / "backend"), "--host", "127.0.0.1", "--port", str(PORT)]
    subprocess.run(["open", "-a", str(APP),
                    "--env", "OMNIVOICE_BACKEND_CMD=" + json.dumps(command),
                    "--env", "OMNIVOICE_ASR_BACKEND=mlx-whisper",
                    "--env", "OMNIVOICE_MLX_FORCED_ALIGN=0"], check=True)
    for _ in range(60):
        if listening():
            print(f"VoiceStudio answers on port {PORT}.")
            return 0
        time.sleep(2)
    sys.exit(f"VoiceStudio did not answer on port {PORT} within 2 minutes.")


if __name__ == "__main__":
    sys.exit(main())
