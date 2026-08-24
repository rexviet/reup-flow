/**
 * SPIKE 2 (GATE) -- phase-00-skeleton-spike.md sec 0.5 / solution design sec 8.
 *
 * Question this answers: does the REAL parser process pick up a cookie
 * written to Redis by Node, on the very next request, with NO restart?
 *
 * This file is deliberately named `*.spike-test.ts` (not `*.spec.ts` /
 * `*.e2e-spec.ts`) so the ordinary `pnpm -C services/downloader test` run
 * (jest.config.js testRegex) never picks it up -- it requires a live docker
 * compose stack and must only run via `run.sh` / `npm run test:spike-cookie-swap`,
 * which uses jest.spike.config.js explicitly.
 *
 * Not a unit test: this is one continuous, ordered scenario against a single
 * long-lived container, so state (redis client, container/worker identity,
 * capture log read cursor) is intentionally shared across `it()` blocks via
 * `ctx`. Jest runs `it()`s within one file strictly in declaration order, so
 * this is safe.
 */
import Redis from "ioredis";
import {
  callParser,
  inspectContainer,
  inspectWorker,
  triggerWorkerRespawn,
  readAllCaptured,
  sleep,
  waitForCapturedCount,
  type ContainerState,
  type WorkerState,
} from "./helpers";

const PARSER_BASE_URL = process.env.SPIKE_PARSER_URL ?? "http://localhost:18080";
const REDIS_URL = process.env.SPIKE_REDIS_URL_HOST ?? "redis://localhost:16379/0";
// Mutation 1 point: overriding this to "douyin:cookie:wrong" must turn this
// whole file RED, because the parser only ever reads "douyin:cookie:current".
const COOKIE_KEY = process.env.SPIKE_COOKIE_KEY ?? "douyin:cookie:current";
const FALLBACK_SENTINEL = process.env.SPIKE_FALLBACK_COOKIE ?? "spike_fallback_sentinel_C";
const CONTAINER_NAME = process.env.SPIKE_CONTAINER_NAME ?? "spike_cookie_parser";
// Mutation 3 point (mutations/03-restart-parser.sh): when set to "1", a
// dedicated it() block below kills the container's serving worker process
// (NOT the container) between steps 2 and 3, reproducing exactly the
// false-green an adversarial review found live. No-op otherwise.
const INJECT_WORKER_RESTART = process.env.SPIKE_INJECT_WORKER_RESTART === "1";

const SENTINEL_A = "spike_sentinel_A=aaaa1111";
const SENTINEL_B = "spike_sentinel_B=bbbb2222";
// How long the echo server holds each step-3 request open before responding.
// Large relative to local dispatch jitter (dispatching 5 fetch() calls takes
// low single-digit ms) so the overlap assertions below are not flaky.
const STEP3_HOLD_MS = 400;

jest.setTimeout(60_000);

describe("SPIKE 2 (GATE): NestJS writes Redis -> parser uses new cookie, no restart", () => {
  const ctx: {
    redis?: Redis;
    containerBefore?: ContainerState;
    workerBefore?: WorkerState;
    captured: number;
  } = {
    captured: 0,
  };

  beforeAll(async () => {
    ctx.redis = new Redis(REDIS_URL, { maxRetriesPerRequest: 2 });
    ctx.captured = readAllCaptured().length;
    // Anti-false-green requirement: record identity BEFORE step 1 so we can
    // prove, after step 4, that the process which answered every request
    // was the SAME process the whole time -- not a harness (or an
    // undetected uvicorn --reload respawn) that quietly restarted between
    // steps. Two levels of identity are captured because they catch
    // different failure modes:
    //  - containerBefore (.State.Pid / .State.StartedAt): catches the whole
    //    container being recreated.
    //  - workerBefore (/proc/[pid]/stat starttime of the multiprocessing-fork
    //    worker): catches the uvicorn --reload supervisor respawning just the
    //    serving worker, which leaves containerBefore's fields untouched --
    //    this is the blind spot an adversarial review proved live by killing
    //    that worker and finding .State.Pid/.StartedAt byte-identical.
    ctx.containerBefore = await inspectContainer(CONTAINER_NAME);
    expect(ctx.containerBefore.running).toBe(true);
    ctx.workerBefore = await inspectWorker(CONTAINER_NAME);
  });

  afterAll(async () => {
    await ctx.redis?.quit();
  });

  it("step 1: Node writes sentinel A, the parser's next request carries A", async () => {
    await ctx.redis!.set(COOKIE_KEY, SENTINEL_A);

    await callParser(PARSER_BASE_URL, "step1-aweme");
    const all = await waitForCapturedCount(ctx.captured + 1);
    const record = all[all.length - 1];
    ctx.captured = all.length;

    expect(record.cookie).toBe(SENTINEL_A);
  });

  it("step 2: Node writes sentinel B with NO restart, the very next request carries B", async () => {
    await ctx.redis!.set(COOKIE_KEY, SENTINEL_B);

    await callParser(PARSER_BASE_URL, "step2-aweme");
    const all = await waitForCapturedCount(ctx.captured + 1);
    const record = all[all.length - 1];
    ctx.captured = all.length;

    expect(record.cookie).toBe(SENTINEL_B);
  });

  it(
    "mutation hook: optionally trigger a legitimate uvicorn --reload respawn of the serving worker mid-scenario " +
      "(no-op unless SPIKE_INJECT_WORKER_RESTART=1)",
    async () => {
      if (!INJECT_WORKER_RESTART) {
        return;
      }

      const before = ctx.workerBefore!;
      // eslint-disable-next-line no-console -- evidence for the human reading test output
      console.log(
        `mutation hook: triggering a reload respawn mid-scenario (SPIKE_INJECT_WORKER_RESTART=1, ` +
          `current worker pid=${before.pid}, starttime=${before.starttime})`,
      );
      await triggerWorkerRespawn(CONTAINER_NAME);

      // Poll until a DIFFERENT worker identity appears AND the parser is
      // answering requests again through it -- both, not just the process
      // existing, since StatReload briefly has no worker at all mid-respawn.
      const deadline = Date.now() + 20_000;
      let after: { pid: number; starttime: string } | undefined;
      while (Date.now() < deadline) {
        try {
          const candidate = await inspectWorker(CONTAINER_NAME);
          if (candidate.pid !== before.pid || candidate.starttime !== before.starttime) {
            await callParser(PARSER_BASE_URL, "post-reload-probe");
            after = candidate;
            break;
          }
        } catch {
          // Worker briefly absent mid-respawn, or the parser isn't
          // answering yet -- keep polling.
        }
        await sleep(200);
      }
      if (!after) {
        throw new Error(
          "parser never respawned a new worker (with a different pid/starttime) after the injected reload trigger",
        );
      }
      // eslint-disable-next-line no-console
      console.log(`mutation hook: confirmed new worker pid=${after.pid}, starttime=${after.starttime}`);
      // Re-sync the capture cursor: the successful probe above appended a
      // record that step 3's `before` baseline must account for.
      ctx.captured = readAllCaptured().length;
    },
  );

  it("step 3: 5 concurrent requests, provably still in flight when the key is swapped mid-batch", async () => {
    // Deterministic starting point for this scenario.
    await ctx.redis!.set(COOKIE_KEY, SENTINEL_A);

    const before = ctx.captured;

    // Each entry's `start`/`end` are Node-side wall-clock timestamps
    // bracketing the fetch(); the echo server holds the TCP connection open
    // for STEP3_HOLD_MS before responding (see echo_server.py), so `end` is
    // never earlier than `start + STEP3_HOLD_MS`. That is what makes the
    // overlap assertions below evidence rather than an assumption about
    // scheduling order.
    interface Dispatched {
      id: string;
      start: number;
      end?: number;
      promise: Promise<unknown>;
    }
    const dispatched: Dispatched[] = [];
    const fire = (id: string): Dispatched => {
      const entry: Dispatched = { id, start: Date.now(), promise: Promise.resolve() };
      entry.promise = callParser(PARSER_BASE_URL, `${id}_hold${STEP3_HOLD_MS}`).then((r) => {
        entry.end = Date.now();
        return r;
      });
      dispatched.push(entry);
      return entry;
    };

    // req0, req1 go out BEFORE the swap below is even issued.
    const req0 = fire("concurrent-0");
    const req1 = fire("concurrent-1");

    // Wait until the echo server has actually RECEIVED both req0 and req1
    // (and is now holding their sockets open for STEP3_HOLD_MS -- see
    // echo_server.py) before swapping the key. This is the fix for the
    // original false green ("swap lands before request 0 even sends
    // headers"): cookie_provider resolves the Cookie header, inside the
    // parser, strictly BEFORE the outbound request to echo is made, so
    // seeing req0/req1 land in captured.log is causal proof their cookie
    // was already resolved from sentinel A -- not a hopeful assumption
    // about Node/asyncio scheduling order.
    const before2 = await waitForCapturedCount(before + 2);
    expect(before2.length).toBeGreaterThanOrEqual(before + 2);

    const swapTime = Date.now();
    // The mid-flight swap. Awaited, so by the time req2-4 are dispatched,
    // Redis is GUARANTEED to already hold B -- that makes the "req2-4 must
    // be B" assertion below deterministic, not a race.
    await ctx.redis!.set(COOKIE_KEY, SENTINEL_B);

    // Not bound: fire() already records each entry in `dispatched`, which is
    // what the overlap assertions below read. Only req0/req1 need names,
    // because they carry the pre-swap in-flight assertion.
    fire("concurrent-2");
    fire("concurrent-3");
    fire("concurrent-4");

    await Promise.all(dispatched.map((d) => d.promise));

    // --- Genuine overlap evidence (fixes the FG-4 false green) ---------
    // req0/req1 were dispatched before the swap AND completed after it: the
    // swap landed while they were provably still in flight, not merely
    // "before some other request in program order".
    for (const entry of [req0, req1]) {
      expect(entry.start).toBeLessThan(swapTime);
      expect(entry.end!).toBeGreaterThan(swapTime);
    }
    // The whole 5-request batch overlapped: the last dispatch happened
    // before the first completion.
    const maxStart = Math.max(...dispatched.map((d) => d.start));
    const minEnd = Math.min(...dispatched.map((d) => d.end!));
    expect(maxStart).toBeLessThan(minEnd);
    // eslint-disable-next-line no-console -- evidence for the human reading test output
    console.log(
      "step3 overlap evidence:",
      dispatched.map((d) => `${d.id}=[${d.start - swapTime}ms,${(d.end! - swapTime)}ms]`).join(", "),
      `swapTime=0 maxStart=${maxStart - swapTime}ms minEnd=${minEnd - swapTime}ms`,
    );
    // ---------------------------------------------------------------------

    // waitForCapturedCount returns as soon as >= the requested count exists
    // (it polls, it does not snapshot), so assert ">=", not "===", against
    // the raw tail -- then select exactly the 5 records this batch cares
    // about by aweme_id. A stray extra record (e.g. from the mutation hook
    // above, already accounted for via `before`, or from any other source)
    // must never turn this gate red for a non-reason.
    const all = await waitForCapturedCount(before + 5);
    expect(all.length).toBeGreaterThanOrEqual(before + 5);
    ctx.captured = all.length;

    const ids = ["concurrent-0", "concurrent-1", "concurrent-2", "concurrent-3", "concurrent-4"];
    const byAweme = new Map(all.slice(before).map((r) => [r.aweme_id, r]));
    const batch = ids.map((id) => {
      const rec = byAweme.get(id);
      expect(rec).toBeDefined();
      return rec!;
    });

    // Core assertion: every one of the 5 captured cookies is a whole,
    // untorn sentinel value -- either A or B, never empty, never a mix of
    // both concatenated, never "<absent>".
    for (const rec of batch) {
      expect([SENTINEL_A, SENTINEL_B]).toContain(rec.cookie);
    }

    // req0/req1 were in flight at swap time but dispatched (and, per the
    // real cookie_provider contract, had already read the cookie for their
    // own request) before it -- they must carry A.
    for (const id of ["concurrent-0", "concurrent-1"]) {
      expect(byAweme.get(id)!.cookie).toBe(SENTINEL_A);
    }
    // req2-4 were dispatched strictly after the awaited swap -- deterministic
    // (see comment above), must carry B.
    for (const id of ["concurrent-2", "concurrent-3", "concurrent-4"]) {
      expect(byAweme.get(id)!.cookie).toBe(SENTINEL_B);
    }

    // eslint-disable-next-line no-console -- evidence for the human reading test output
    console.log(
      "step3 concurrency split:",
      batch.map((r) => `${r.aweme_id}=${r.cookie === SENTINEL_A ? "A" : "B"}`).join(", "),
    );

    // After the swap has settled, a fresh request must carry B.
    await callParser(PARSER_BASE_URL, "concurrent-settled");
    const afterSettle = await waitForCapturedCount(ctx.captured + 1);
    ctx.captured = afterSettle.length;
    expect(afterSettle[afterSettle.length - 1].cookie).toBe(SENTINEL_B);
  });

  it("step 4: deleting the key does not crash the parser and it falls back to DOUYIN_COOKIE_FALLBACK", async () => {
    await ctx.redis!.del(COOKIE_KEY);

    await callParser(PARSER_BASE_URL, "step4-aweme");
    const all = await waitForCapturedCount(ctx.captured + 1);
    const record = all[all.length - 1];
    ctx.captured = all.length;

    expect(record.cookie).toBe(FALLBACK_SENTINEL);

    const state = await inspectContainer(CONTAINER_NAME);
    expect(state.running).toBe(true);
  });

  it("anti-restart: the SERVING WORKER's identity is unchanged from before step 1 to after step 4", async () => {
    // Primary evidence (fixes the FG-5 false green): this is the check that
    // actually catches what container-level Pid/StartedAt cannot. See the
    // long comment in beforeAll and in helpers.ts:inspectWorker.
    const workerAfter = await inspectWorker(CONTAINER_NAME);
    expect(workerAfter.pid).toBe(ctx.workerBefore!.pid);
    expect(workerAfter.starttime).toBe(ctx.workerBefore!.starttime);
  });

  it("anti-restart (secondary signal): container start time and main PID are unchanged from before step 1 to after step 4", async () => {
    // Kept as a secondary signal (catches whole-container recreation), but
    // per FG-5 it must never again be the ONLY evidence -- the worker-level
    // check above is now primary.
    const containerAfter = await inspectContainer(CONTAINER_NAME);

    expect(containerAfter.pid).toBe(ctx.containerBefore!.pid);
    expect(containerAfter.startedAt).toBe(ctx.containerBefore!.startedAt);
  });
});
