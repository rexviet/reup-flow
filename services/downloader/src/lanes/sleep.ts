/**
 * `await`s `ms`, or returns early if `signal` aborts -- whichever happens
 * first. Never rejects.
 *
 * This is the only wait the lane loop performs (`lane.runner.ts`), and every
 * clause above is load-bearing.
 *
 * EARLY RETURN ON ABORT. The reaper's idle delay is 60s (§4.3). If a shutdown
 * had to wait out a running timer, `docker stop` -- 10s by default -- would
 * give up and `SIGKILL` instead, skipping the release-the-lease step §4.2
 * exists for. The lease would then hold for its full `LEASE_SECONDS` and the
 * restarted process would sit there with nothing to claim, which is the exact
 * symptom §4.2's DoD line forbids.
 *
 * RESOLVES, NEVER REJECTS. The lane loop wraps its whole body in a
 * `try/catch` -- that catch is this phase's headline guard -- so a rejection
 * on the shutdown path would be caught there, logged as a lane failure, and
 * answered with an error-backoff sleep. Shutdown would read as a crash in the
 * logs and take an extra backoff on the way out. Keeping "we are stopping"
 * and "something went wrong" as two distinct signals is worth the swallow.
 *
 * WHY NOT `node:timers/promises`. Its `setTimeout` accepts a signal and does
 * all of this, and this function was written over it first. Two reasons it is
 * not:
 *
 *   1. It rejects with a `DOMException` created in NODE's realm. Recognising
 *      that with `err instanceof Error` is false under jest, whose test file
 *      runs in a different realm with a different `Error` -- so the abort was
 *      rethrown instead of swallowed and every abort-during-wait assertion in
 *      sleep.spec.ts failed. A cross-realm `instanceof` is a trap, not a
 *      one-off; duck-typing on `name` would have fixed it, but see 2.
 *   2. Its timer and its listener are Node's, registered against Node's own
 *      `clearTimeout`. The two hygiene assertions in sleep.spec.ts -- no timer
 *      left pending, no listener left on the signal -- then have nothing they
 *      can observe, and a test that cannot go red is not evidence (plan §5).
 *      Owning the timer here is what makes those two assertions real.
 *
 * The hygiene matters because the loop calls this thousands of times an hour
 * against ONE long-lived signal: a listener added per call and never removed
 * is an unbounded array on a process-lifetime object, and a timer left
 * pending is a process that will not exit.
 */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  // Checked before a timer or a listener exists at all: the loop calls this
  // immediately after a tick, and an abort that arrived during that tick
  // should cost nothing.
  if (signal?.aborted) return Promise.resolve();

  return new Promise<void>((resolve) => {
    // Both paths run `settle`, and `settle` is what removes the listener and
    // clears the timer. Whichever of the two fires first, neither the timer
    // nor the listener outlives this call.
    const onAbort = (): void => settle();
    const timer = setTimeout(settle, ms);

    function settle(): void {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }

    signal?.addEventListener('abort', onAbort, { once: true });
  });
}
