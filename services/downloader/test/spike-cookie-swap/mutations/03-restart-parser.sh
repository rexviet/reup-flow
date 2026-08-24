#!/usr/bin/env bash
# Required mutation 3 (FG-5 fix, see phase-00-skeleton-spike.md sec 0.5 report).
#
# Reproduces, on demand, the exact false-green an adversarial review found
# live: the container's PID 1 (`/bin/sh ./start.sh`) never restarts, but the
# process that actually serves requests -- the uvicorn --reload worker
# (`python3 ... spawn_main --multiprocessing-fork`) -- respawns mid-scenario.
# The review found `docker inspect`'s `.State.Pid` / `.State.StartedAt`
# byte-identical before/after that respawn, which is exactly the blind spot
# the worker-identity check in helpers.ts:inspectWorker (compared in
# cookie-swap.spike-test.ts's "anti-restart: the SERVING WORKER's identity
# is unchanged" test) exists to close.
#
# Applied change (printed for the record): env var
# SPIKE_INJECT_WORKER_RESTART=1, read by cookie-swap.spike-test.ts's
# dedicated "mutation hook" it() block (runs between steps 2 and 3), which
# docker-execs `touch /app/start.py` inside the running parser container --
# bumping the mtime of a real, already-imported .py file that uvicorn's
# StatReload watches, which drives its OWN should_restart()/restart() logic
# (uvicorn/supervisors/basereload.py) to terminate the current worker and
# spawn a fresh one, entirely inside the running container. (A raw SIGKILL
# on the worker was tried first and does NOT work: StatReload has no
# child-health-check / auto-heal logic, so killing the worker directly just
# leaves the app permanently dead -- which is, not coincidentally, exactly
# what the adversarial review observed live; see the long comment on
# helpers.ts:triggerWorkerRespawn.) Steps 1-4 (cookie swap semantics,
# fallback-on-delete) are UNAFFECTED by a worker respawn -- only
# worker-identity anti-restart must go RED. The container-level anti-restart
# check is expected to stay GREEN here (the container itself is never
# touched), which is exactly why it alone was insufficient (FG-5).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== MUTATION 3: applied config change ==="
echo "SPIKE_INJECT_WORKER_RESTART=1   (mid-scenario: kill the serving multiprocessing-fork worker inside the parser container; container/PID 1 untouched)"
echo

export SPIKE_INJECT_WORKER_RESTART=1
echo ">>> expect the worker-identity anti-restart check RED, steps 1-4 + container-level anti-restart unaffected"
if ./run.sh; then
  echo "::error::mutation did not turn the gate red -- the worker-identity guard is vacuous"
  exit 1
fi
echo "confirmed RED as expected"

echo
echo "=== restoring: unset SPIKE_INJECT_WORKER_RESTART ==="
unset SPIKE_INJECT_WORKER_RESTART
echo ">>> expect all green again"
./run.sh
