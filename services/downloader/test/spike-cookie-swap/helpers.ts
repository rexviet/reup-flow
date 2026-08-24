/**
 * Shared helpers for the SPIKE 2 (GATE) jest harness. Kept out of
 * `services/downloader/src/**` on purpose -- this is throwaway spike
 * plumbing, not production code, and must not be picked up by the main
 * jest config (see jest.spike.config.js / cookie-swap.spike-test.ts naming).
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import * as fs from "node:fs";
import * as path from "node:path";

const execFileAsync = promisify(execFile);

export const CAPTURED_LOG_PATH = path.join(__dirname, "data", "captured.log");

export interface CapturedRecord {
  seq: number;
  ts: number;
  method: string;
  path: string;
  aweme_id: string;
  cookie: string;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Parses whatever is currently on disk. Tolerates the file not existing yet,
 * and tolerates a partially-flushed trailing line: the bind-mount read here
 * can race the echo server's write()+fsync() (see waitForCapturedCount,
 * which polls precisely because of that race), so a line that fails to
 * parse is skipped rather than thrown -- the caller's poll loop will pick it
 * up whole on the next tick once the write settles. A skipped line never
 * masks a real failure because every assertion site keys off specific
 * `aweme_id`s / counts, not "whatever JSON.parse produced this tick".
 */
export function readAllCaptured(): CapturedRecord[] {
  if (!fs.existsSync(CAPTURED_LOG_PATH)) return [];
  const raw = fs.readFileSync(CAPTURED_LOG_PATH, "utf8");
  const records: CapturedRecord[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (trimmed.length === 0) continue;
    try {
      records.push(JSON.parse(trimmed) as CapturedRecord);
    } catch {
      // Unparseable trailing line -- likely a partial flush. Skip; the poll
      // loop in waitForCapturedCount will see it complete on a later read.
    }
  }
  return records;
}

/**
 * Polls the bind-mounted capture log until at least `minTotalCount` records
 * exist, or times out. Polling (not a single read-after-await) matters
 * because on Docker Desktop the container->host bind-mount propagation is
 * not guaranteed to be visible the instant the container's fsync() returns.
 */
export async function waitForCapturedCount(
  minTotalCount: number,
  timeoutMs = 10_000,
): Promise<CapturedRecord[]> {
  const deadline = Date.now() + timeoutMs;
  let last: CapturedRecord[] = [];
  while (Date.now() < deadline) {
    last = readAllCaptured();
    if (last.length >= minTotalCount) return last;
    await sleep(50);
  }
  throw new Error(
    `timed out waiting for captured.log to reach ${minTotalCount} records ` +
      `(has ${last.length} after ${timeoutMs}ms): ${JSON.stringify(last)}`,
  );
}

export interface ParserCallResult {
  status: number;
  body: unknown;
}

/**
 * Calls the real parser's fetch_one_video route. A 200 or 400 both mean
 * "the parser answered" -- 400 is EXPECTED because the echo server's body is
 * not real Douyin data (see echo_server.py docstring). Anything else
 * (connection refused, timeout, 5xx) is treated as a genuine failure by the
 * caller via a thrown error, since that could indicate a real crash.
 */
export async function callParser(
  baseUrl: string,
  awemeId: string,
): Promise<ParserCallResult> {
  const url = `${baseUrl}/api/douyin/web/fetch_one_video?aweme_id=${encodeURIComponent(awemeId)}`;
  const res = await fetch(url, { method: "GET" });
  const body = await res.json().catch(() => undefined);
  if (res.status >= 500) {
    throw new Error(`parser returned ${res.status} for ${url}: ${JSON.stringify(body)}`);
  }
  return { status: res.status, body };
}

export interface ContainerState {
  startedAt: string;
  pid: number;
  running: boolean;
}

/** Reads the container's start time + main PID via `docker inspect`. */
export async function inspectContainer(containerName: string): Promise<ContainerState> {
  const { stdout } = await execFileAsync("docker", [
    "inspect",
    containerName,
    "--format",
    "{{.State.StartedAt}}|{{.State.Pid}}|{{.State.Running}}",
  ]);
  const [startedAt, pidStr, runningStr] = stdout.trim().split("|");
  return {
    startedAt,
    pid: Number(pidStr),
    running: runningStr === "true",
  };
}

export interface WorkerState {
  pid: number;
  /** /proc/[pid]/stat field 22 -- starttime in jiffies since boot. Monotonic
   *  and immune to PID reuse, unlike wall-clock time or the PID alone. */
  starttime: string;
}

/**
 * Finds, inside `containerName`, the identity of the process that ACTUALLY
 * serves requests -- not the container's PID 1.
 *
 * start.py runs `uvicorn.run(..., reload=True)`. That makes the container's
 * process tree:
 *   1   /bin/sh ./start.sh                              <- .State.Pid
 *   7   python3 start.py                                <- reload supervisor
 *   19  python3 ... spawn_main --multiprocessing-fork    <- serves requests
 *
 * `docker inspect`'s `.State.Pid` / `.State.StartedAt` (see inspectContainer)
 * only describe PID 1 and are unchanged when the reload supervisor respawns
 * PID 19 -- confirmed live: an adversarial review killed PID 19 and found
 * `.State.Pid` / `.State.StartedAt` byte-identical before/after while the app
 * was completely dead in between. This function is the fix for that blind
 * spot: it must be compared before step 1 and after step 4, in addition to
 * (not instead of) inspectContainer.
 *
 * Implemented as a single `docker exec ... python3 -c` scan of /proc rather
 * than `pgrep`/`pkill`, because the parser image (`python:3.11-slim`) is not
 * guaranteed to ship procps -- python3 is the one binary certain to exist in
 * a Python application container.
 */
export async function inspectWorker(containerName: string): Promise<WorkerState> {
  const { stdout } = await execFileAsync("docker", [
    "exec",
    containerName,
    "python3",
    "-c",
    FIND_WORKER_PY,
  ]);
  const [pidStr, starttime] = stdout.trim().split(/\s+/);
  if (!pidStr || !starttime) {
    throw new Error(
      `inspectWorker: could not resolve the serving worker inside ${containerName}; ` +
        `docker exec output was: ${JSON.stringify(stdout)}`,
    );
  }
  return { pid: Number(pidStr), starttime };
}

/**
 * Triggers a LEGITIMATE uvicorn --reload respawn of the serving worker,
 * leaving the container and its reloader supervisor (PID 1 / PID of
 * `python3 start.py`) untouched. Used ONLY by the SPIKE_INJECT_WORKER_RESTART
 * mutation hook (see cookie-swap.spike-test.ts + mutations/03-restart-parser.sh)
 * to reproduce, on demand, the exact false-green scenario the adversarial
 * review found live: a worker respawn between steps that container-level
 * `docker inspect` fields cannot see.
 *
 * This bumps the mtime of `start.py` (a real, already-imported .py file
 * uvicorn's StatReload watches under `/app`), which drives StatReload's OWN
 * `should_restart()` polling loop to detect a change and call `restart()`
 * (uvicorn/supervisors/basereload.py): the current worker subprocess is
 * terminated and a fresh one is spawned, all inside the running container.
 *
 * A prior version of this mutation SIGKILLed the worker directly. That does
 * NOT work: uvicorn's StatReload has no child-health-check / auto-heal
 * logic (confirmed by reading basereload.py -- its run loop only reacts to
 * `should_restart()`, never to the child dying), so a raw kill just leaves
 * the app permanently dead with no worker to answer requests ever again --
 * which is, not coincidentally, exactly what the adversarial review
 * observed live. Only a real reload trigger reproduces "worker respawns,
 * container doesn't" without also killing the app outright.
 */
export async function triggerWorkerRespawn(containerName: string): Promise<void> {
  // Touch twice, a beat apart: StatReload's very first observation of a
  // given file only RECORDS a baseline mtime, it doesn't compare (see
  // should_restart() in basereload.py) -- so a single touch that happens to
  // race that first observation would be silently absorbed as the new
  // baseline instead of registering as a change. Two touches guarantee at
  // least the second one lands after a baseline already exists.
  await execFileAsync("docker", ["exec", containerName, "touch", "/app/start.py"]);
  await sleep(300);
  await execFileAsync("docker", ["exec", containerName, "touch", "/app/start.py"]);
}

// NOTE on matching strategy: do NOT substring-search the raw cmdline text
// for "multiprocessing-fork". A `docker exec <c> python3 -c "<this script>"`
// invocation's OWN /proc/<pid>/cmdline contains this script's source, which
// itself contains the string "multiprocessing-fork" as a literal -- a naive
// substring check matches the invocation of this very script, not the real
// worker, and since the exec spawns a fresh (higher) PID every single call,
// "pick the highest matching PID" then ALWAYS returns a brand-new PID that
// looks like a restart on every call. (Caught live: workerBefore/workerAfter
// differed on a completely unmutated run because of exactly this.) The real
// spawn_main worker's argv, from /proc/<pid>/cmdline split on NUL, ends with
// the flag as its OWN, separate argv element -- e.g.
// ["/usr/local/bin/python3", "-c", "from multiprocessing.spawn import ...", "--multiprocessing-fork"].
// Requiring an EXACT match on the last argv element excludes both this
// script's own invocation and multiprocessing.resource_tracker (whose argv
// never ends with that flag).
const FIND_WORKER_PY = `
import os
matches = []
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        with open(f"/proc/{entry}/cmdline", "rb") as f:
            raw = f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    argv = [a for a in raw.decode(errors="replace").split("\\0") if a != ""]
    if not argv or argv[-1] != "--multiprocessing-fork":
        continue
    with open(f"/proc/{entry}/stat", "r") as f:
        stat = f.read()
    after_comm = stat[stat.rfind(")") + 1:].strip()
    fields = after_comm.split()
    # stat field 1 = pid, field 2 = comm (the parenthesized part we just
    # split off); fields[] here starts at field 3 (state), so field 22
    # (starttime) is index 22 - 3 = 19.
    starttime = fields[19]
    matches.append((int(entry), starttime))
if not matches:
    raise SystemExit(1)
matches.sort()
pid, starttime = matches[-1]
print(f"{pid} {starttime}")
`.trim();
