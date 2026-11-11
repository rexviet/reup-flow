#!/usr/bin/env bash
# Prints the review link (with a fresh grant) for the project HyperFrames Studio has open.
# `scripts/remote-studio.sh` makes this the only command the remote key may run, so someone
# viewing from outside the LAN, who has no shell, can still get the link:
#   ssh -p <port> macbook@<host>
# It ignores whatever command the client asked for.
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.."

# The identity comes from the running Studio launcher: `review-studio.mjs open <aweme> <generation>`.
read -r aweme generation < <(
  ps -axo args | awk '/review-studio\.mjs open [0-9]+ [0-9]+/ && !/awk/ {for (i=1;i<=NF;i++) if ($i=="open") {print $(i+1), $(i+2); exit}}'
) || true
[ -n "${aweme:-}" ] && [ -n "${generation:-}" ] || { echo "no Studio is open on this machine" >&2; exit 1; }

json=$(docker compose exec -T review-bridge python -m localization.review_bridge.cli launch "$aweme" "$generation")
grant=$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().strip().splitlines()[-1])["grant"])')
echo "Open this on the machine you are viewing from, while the ssh -L tunnel is running:"
echo "  http://127.0.0.1:3310/#$grant"
echo "(project $aweme, generation $generation)"
