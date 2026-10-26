#!/usr/bin/env bash
# Usage: run.sh <command...>   e.g.  run.sh uv run python g1_durable_agent.py
# Runs inside the compose network. Only the variables named below are passed
# through, by name (values come from the caller's environment, never argv).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
docker build -q -t reup-spikes "$here" >/dev/null

pass=(TEMPORAL_ADDRESS TEMPORAL_NAMESPACE POSTGRES_USER POSTGRES_PASSWORD
      LANGFUSE_HOST LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
      OPENROUTER_BASE_URL OPENROUTER_API_KEY G1_OPENROUTER_MODELS
      VOICESTUDIO_URL VOICESTUDIO_API_KEY VOICE_ID STT_MODEL TTS_MODEL OCR_MODEL)
envargs=()
for v in "${pass[@]}"; do
  if [ -n "${!v:-}" ]; then envargs+=(-e "$v"); fi
done

exec docker run --rm --network "${COMPOSE_NETWORK:-reup-flow_default}" \
  -v "$here":/spikes -v "$repo/data":/data:ro \
  -v reup-spikes-uv-cache:/root/.cache/uv -v reup-spikes-venv:/opt/venv -v reup-spikes-nm:/spikes/node_modules \
  ${envargs[@]+"${envargs[@]}"} reup-spikes "$@"
