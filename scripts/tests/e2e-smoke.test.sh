#!/usr/bin/env bash
# Tests terminal/exit orchestration. Real provider/MinIO checks remain separate.
set -euo pipefail
cd "$(dirname "$0")/../.."
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
cat > "$scratch/docker" <<'DOCKER'
#!/usr/bin/env bash
case "$*" in
  *'output_key IS NULL'*) echo "${BUNDLE_VALID:-t}" ;;
  *'SELECT generation '*) [ "${DB_ERROR:-0}" = 0 ] || exit 1; echo "2 $TERMINAL" ;;
  *'SELECT status FROM videos'*) echo done ;;
  *verify_delivery*) exit "${VERIFY_EXIT:-0}" ;;
  *) echo 'unexpected command in smoke test' >&2; exit 99 ;;
esac
DOCKER
chmod +x "$scratch/docker"
check() {
  local expected="$1"; shift
  local code=0
  PATH="$scratch:$PATH" bash scripts/e2e-smoke.sh "$@" > "$scratch/log" 2>&1 || code=$?
  [ "$code" = "$expected" ] || { cat "$scratch/log"; echo "Expected $expected, got $code"; exit 1; }
}
export TERMINAL=ready_for_review
check 0 --run 7001 2 1
export TERMINAL=needs_human
check 4 --run 7001 2 1
check 1 --mode legacy_render --run 7001 2 1
export TERMINAL=published
check 1 --run 7001 2 1
check 0 --mode legacy_render --run 7001 2 1
export TERMINAL=ready_for_review BUNDLE_VALID=f
check 1 --run 7001 2 1
export BUNDLE_VALID=t VERIFY_EXIT=1
check 1 --run 7001 2 1
export DB_ERROR=1
check 2 --run 7001 2 1
check 2 --run invalid 2 1
echo 'PASS: review success, human correction exit 4, explicit legacy, wrong mode, missing/corrupt bundle and invalid identity'
