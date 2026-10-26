#!/usr/bin/env bash
#
# Mutation suite for the localization-infra guards in
# test/compose-constraints.spec.ts (docker-compose.yml + infra/minio/Dockerfile).
#
# A guard is evidence only if breaking the invariant turns it red. Each mutation
# below breaks one line of the compose file and REQUIRES the spec to fail; each
# control changes a line that is NOT an invariant and REQUIRES it to stay green,
# which is what says the spec is pinning the property and not the whole file.
#
# Rules this harness follows (the same ones the other harnesses learned):
#   1. A mutation whose diff is empty is a hard error, never a pass.
#   2. Positive evidence that tests ran ("Tests: N ... total", N > 0). Exit code
#      alone cannot tell "caught" from "selected nothing".
#   3. Files are restored from a pristine snapshot on ANY exit, and a restore
#      that did not restore is fatal.
#   4. Every run is bounded by a timeout.
#
# Run from services/downloader:  bash test/mutations/phase-infra.sh

set -u

cd "$(dirname "$0")/../.." || exit 1

COMPOSE=../../docker-compose.yml
MINIO_DOCKERFILE=../../infra/minio/Dockerfile
PATTERN=compose-constraints
JEST_TIMEOUT_SECONDS=120

SNAP=$(mktemp -d)
cp "$COMPOSE" "$SNAP/compose.yml"
cp "$MINIO_DOCKERFILE" "$SNAP/Dockerfile"
fail=0
# Outcome counters. The final verdict is arithmetic over these, not an absence
# of FAIL lines: a harness that dies before running a mutation prints no FAIL.
EXPECT_MUTATIONS=11
EXPECT_CONTROLS=2
caught=0
green=0

restore_all() {
  cp "$SNAP/compose.yml" "$COMPOSE"
  cp "$SNAP/Dockerfile" "$MINIO_DOCKERFILE"
}
verify_restored() {
  if ! diff -q "$SNAP/compose.yml" "$COMPOSE" >/dev/null || ! diff -q "$SNAP/Dockerfile" "$MINIO_DOCKERFILE" >/dev/null; then
    echo "FATAL: failed to restore the pristine files"
    exit 1
  fi
}
on_exit() {
  if ! diff -q "$SNAP/compose.yml" "$COMPOSE" >/dev/null || ! diff -q "$SNAP/Dockerfile" "$MINIO_DOCKERFILE" >/dev/null; then
    echo "NOTE: restored from the pristine snapshot"
    restore_all
  fi
  rm -rf "$SNAP"
}
trap on_exit EXIT

# run_jest: 0 green, 1 red, 98 timeout, 99 nothing ran.
run_jest() {
  pnpm exec jest "$PATTERN" >"$SNAP/out.log" 2>&1 &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$JEST_TIMEOUT_SECONDS" ]; then
      pkill -P "$pid" 2>/dev/null; kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
      return 98
    fi
    sleep 1; waited=$((waited + 1))
  done
  wait "$pid"; local rc=$?
  # Positive evidence at least one test ran.
  grep -qE '^Tests: .*[1-9][0-9]* (passed|failed)' "$SNAP/out.log" || return 99
  [ "$rc" -eq 0 ] && return 0
  return 1
}

# apply <label> <must-fail|control> <mutator...>; prints the applied diff.
apply() {
  local label=$1 kind=$2
  shift 2
  echo
  echo "=============================================================="
  echo "$(printf %s "$kind" | tr a-z A-Z): $label"
  echo "=============================================================="
  "$@"
  if diff -q "$SNAP/compose.yml" "$COMPOSE" >/dev/null && diff -q "$SNAP/Dockerfile" "$MINIO_DOCKERFILE" >/dev/null; then
    echo "ERROR: mutation matched nothing -- the code shape changed and this"
    echo "       mutation no longer applies. Update it; do NOT treat this as a pass."
    fail=1
    return 1
  fi
  echo "--- diff actually applied ---"
  diff "$SNAP/compose.yml" "$COMPOSE"
  diff "$SNAP/Dockerfile" "$MINIO_DOCKERFILE"
  echo "-----------------------------"
}

run_mutation() {
  local label=$1
  shift
  if apply "$label" must-fail "$@"; then
    run_jest; local rc=$?
    case $rc in
      99) echo "ERROR: the test pattern '$PATTERN' selected no tests"; fail=1 ;;
      98) echo "OK: caught, but by HANGING -- ${JEST_TIMEOUT_SECONDS}s timeout"; caught=$((caught + 1)) ;;
      0)  echo "FAIL: suite stayed GREEN with the invariant broken -- these tests prove nothing."
          tail -20 "$SNAP/out.log"; fail=1 ;;
      *)  echo "OK: caught. Failing tests:"; grep -E '✕' "$SNAP/out.log" || tail -10 "$SNAP/out.log"
          caught=$((caught + 1)) ;;
    esac
  fi
  restore_all; verify_restored
}

run_control() {
  local label=$1
  shift
  if apply "$label" control "$@"; then
    run_jest; local rc=$?
    case $rc in
      99) echo "ERROR: the test pattern '$PATTERN' selected no tests"; fail=1 ;;
      98) echo "FAIL: control never finished -- a control that hangs is unmeasured."; fail=1 ;;
      0)  echo "OK: still green, as required."; green=$((green + 1)) ;;
      *)  echo "FAIL: control went RED -- the spec pins more than the invariant."
          grep -E '✕' "$SNAP/out.log" || tail -10 "$SNAP/out.log"; fail=1 ;;
    esac
  fi
  restore_all; verify_restored
}

# Edits go through python so a no-match is an empty diff, which apply() rejects.
edit() { # edit <file> <old> <new> [count]
  python3 - "$@" <<'PY'
import pathlib, sys
f, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
count = int(sys.argv[4]) if len(sys.argv) > 4 else 1
p = pathlib.Path(f); s = p.read_text()
p.write_text(s.replace(old, new, count))
PY
}

# --- ports: only three, all on loopback -------------------------------------
run_mutation "langfuse-web published on every interface" \
  edit "$COMPOSE" '"127.0.0.1:3100:3000"' '"3100:3000"'

run_mutation "ClickHouse gains a published port" \
  edit "$COMPOSE" '    mem_limit: 2g
' '    mem_limit: 2g
    ports:
      - "127.0.0.1:8123:8123"
'

run_mutation "MinIO S3 API published next to the console" \
  edit "$COMPOSE" '      - "127.0.0.1:9001:9001"
' '      - "127.0.0.1:9001:9001"
      - "127.0.0.1:9000:9000"
'

# --- environment isolation ---------------------------------------------------
run_mutation "the Langfuse worker is handed the web session secret" \
  edit "$COMPOSE" '  langfuse-worker:
    image: langfuse/langfuse-worker:3@sha256:8a28c946bb5401eef488153fa294db5a79bd99dd5c90db8e4d39559374c9ebd3
    environment:
      <<: *langfuse-env
' '  langfuse-worker:
    image: langfuse/langfuse-worker:3@sha256:8a28c946bb5401eef488153fa294db5a79bd99dd5c90db8e4d39559374c9ebd3
    environment:
      <<: *langfuse-env
      NEXTAUTH_SECRET: ${NEXTAUTH_SECRET}
'

run_mutation "temporal-ui is given env_file: .env" \
  edit "$COMPOSE" '  temporal-ui:
' '  temporal-ui:
    env_file: .env
'

run_mutation "Temporal gets the Postgres superuser password under a renamed key" \
  edit "$COMPOSE" 'POSTGRES_PWD: ${TEMPORAL_DB_PASSWORD}' 'POSTGRES_PWD: ${POSTGRES_PASSWORD}'

# --- supply chain ------------------------------------------------------------
run_mutation "the Temporal server image loses its digest" \
  edit "$COMPOSE" 'temporalio/server:1.29@sha256:688da708d0daca6ff8083334a4a7fbbdbc7e70d1cca8e2eda8c05b21fdbd601e' 'temporalio/server:1.29'

run_mutation "the MinIO init base image loses its digest" \
  edit "$MINIO_DOCKERFILE" 'alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc' 'alpine:3.20'

# --- ordering and data safety ------------------------------------------------
run_mutation "db-init is allowed to restart" \
  edit "$COMPOSE" '    image: postgres:16-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36
    restart: "no"' '    image: postgres:16-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36
    restart: unless-stopped'

run_mutation "Temporal starts once the schema job has merely started" \
  edit "$COMPOSE" '      temporal-admin-tools:
        condition: service_completed_successfully' '      temporal-admin-tools:
        condition: service_started'

run_mutation "redis allowed to evict" \
  edit "$COMPOSE" ' --maxmemory-policy noeviction' ''

# --- controls: not invariants, must stay green --------------------------------
run_control "change the Langfuse public URL" \
  edit "$COMPOSE" 'NEXTAUTH_URL: http://localhost:3100' 'NEXTAUTH_URL: http://localhost:3101'

run_control "raise the ClickHouse memory ceiling" \
  edit "$COMPOSE" 'mem_limit: 2g' 'mem_limit: 3g'

echo
if [ "$caught" -ne "$EXPECT_MUTATIONS" ] || [ "$green" -ne "$EXPECT_CONTROLS" ]; then
  echo "FAIL: caught $caught/$EXPECT_MUTATIONS mutations, $green/$EXPECT_CONTROLS controls green -- the harness did not measure everything it declares."
  fail=1
fi
if [ "$fail" -ne 0 ]; then
  echo "phase-infra: FAILED"
  exit 1
fi
echo "phase-infra: every mutation caught, every control green"
