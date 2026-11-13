#!/usr/bin/env bash
# Installs Demucs for the host ingest worker (scripts/host-ingest-worker.mjs), so the voice is
# split from the music on this Mac's GPU (Apple's MPS) instead of in the media container, which
# only has a CPU and shares it with everything else.
#
#   scripts/setup-host-demucs.sh
#
# What it makes, under services/localization/.host-demucs/ (not tracked by git):
#   env/          Python with torch 2.6.0, torchaudio, demucs 4.0.1 and soundfile. The project's own
#                 environment stays on torch 2.5.1, which MPS cannot run Demucs on (it stops with
#                 "Output channels > 65536 not supported at the MPS device").
#   torch/hub/    the htdemucs weights. The URL and sha256 are the ones in the Dockerfile, read from
#                 it, and the download is refused unless the digest matches, as it is there.
#   demucs-host   the command the worker runs as DEMUCS_BIN: Demucs through the project's
#                 `demucs_safe` loader (weights_only=True), on MPS, and on the CPU if MPS fails.
# Safe to run again: what is already there is kept.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
DIR="$ROOT/services/localization/.host-demucs"
DOCKERFILE="$ROOT/services/localization/Dockerfile"
if [ "$(uname)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo "this needs an Apple Silicon Mac (MPS)" >&2
  exit 1
fi
command -v uv >/dev/null 2>&1 || { echo "uv is not installed" >&2; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg is not installed" >&2; exit 1; }

dockerfile_arg() { sed -n "s/^ARG $1=//p" "$DOCKERFILE" | head -1; }
URL=$(dockerfile_arg DEMUCS_WEIGHTS_URL)
SHA=$(dockerfile_arg DEMUCS_WEIGHTS_SHA256)
if [ -z "$URL" ] || [ -z "$SHA" ]; then
  echo "the weights URL and sha256 were not found in $DOCKERFILE" >&2
  exit 1
fi
WEIGHTS="$DIR/torch/hub/checkpoints/$(basename "$URL")"

mkdir -p "$DIR/torch/hub/checkpoints"
if [ ! -x "$DIR/env/bin/python" ]; then
  uv venv --python 3.12 "$DIR/env"
fi
OVERRIDES=$(mktemp)
trap 'rm -f "$OVERRIDES"' EXIT
# demucs declares an old torchaudio ceiling; this one bound is overridden, nothing else.
printf 'torchaudio==2.6.0\n' > "$OVERRIDES"
uv pip install --python "$DIR/env/bin/python" --override "$OVERRIDES" \
  torch==2.6.0 torchaudio==2.6.0 demucs==4.0.1 soundfile numpy

if [ ! -f "$WEIGHTS" ]; then
  curl -fsSL -o "$WEIGHTS.part" "$URL"
  mv "$WEIGHTS.part" "$WEIGHTS"
fi
if ! echo "$SHA  $WEIGHTS" | shasum -a 256 -c - >/dev/null; then
  rm -f "$WEIGHTS"
  echo "the weights do not match the recorded sha256; removed" >&2
  exit 1
fi

cat > "$DIR/demucs-host" <<WRAPPER
#!/bin/sh
# Written by scripts/setup-host-demucs.sh.
export TORCH_HOME="$DIR/torch"
export PYTORCH_ENABLE_MPS_FALLBACK=1
export PYTHONPATH="$ROOT/services/localization/src"
PY="$DIR/env/bin/python"
"\$PY" -m localization.providers.demucs_safe -d mps "\$@" && exit 0
echo "demucs on mps failed, trying the cpu" >&2
exec "\$PY" -m localization.providers.demucs_safe -d cpu "\$@"
WRAPPER
chmod 755 "$DIR/demucs-host"

"$DIR/env/bin/python" -c 'import torch; print("torch", torch.__version__, "mps", torch.backends.mps.is_available())'
echo "ready: $DIR/demucs-host"
