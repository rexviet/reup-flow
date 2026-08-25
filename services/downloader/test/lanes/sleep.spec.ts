/**
 * Phase 4 §4.1/§4.2 -- the sleep the lane loop waits on between ticks.
 *
 * It is a separate, tested primitive rather than a bare `setTimeout` inside
 * `lane.runner.ts` because of one requirement that is easy to get wrong and
 * invisible when it is: it MUST come back early on abort. The reaper sleeps
 * 60s between ticks (§4.3). A `SIGTERM` that has to wait out a running
 * `setTimeout` turns a graceful shutdown into a 60-second one, and Docker's
 * default stop timeout is 10 -- so the container is SIGKILLed instead, which
 * skips the whole "release the lease before exiting" step §4.2 exists to
 * guarantee. The lease then holds for its full `LEASE_SECONDS` and the
 * restart sits idle, which is exactly the symptom §4.2 says must not happen.
 *
 * The two hygiene assertions (no timer left behind, no abort listener left
 * behind) are here because the loop calls this thousands of times per hour
 * against ONE long-lived AbortSignal: a listener leak is an
 * ever-growing array on the signal, and a timer left running is a process
 * that will not exit.
 */
import { getEventListeners } from 'node:events';
import { sleep } from '../../src/lanes/sleep';

describe('sleep(ms, signal)', () => {
  it('resolves after roughly the requested delay', async () => {
    const started = Date.now();
    await sleep(40);
    // Lower bound only: timers fire late under load, never early enough to
    // matter, and asserting an upper bound here is how a suite becomes flaky
    // on a busy CI box.
    expect(Date.now() - started).toBeGreaterThanOrEqual(30);
  });

  it('resolves immediately when the signal is ALREADY aborted', async () => {
    const controller = new AbortController();
    controller.abort();

    const started = Date.now();
    await sleep(60_000, controller.signal);

    expect(Date.now() - started).toBeLessThan(200);
  });

  it('resolves early when the signal aborts DURING the wait', async () => {
    const controller = new AbortController();
    const started = Date.now();
    const pending = sleep(60_000, controller.signal);

    setTimeout(() => controller.abort(), 20);
    await pending;

    // The whole point: a 60s sleep must not delay shutdown by 60s.
    expect(Date.now() - started).toBeLessThan(1_000);
  });

  it('RESOLVES on abort rather than rejecting', async () => {
    // Deliberate, and the reason this wraps `node:timers/promises` instead of
    // being used directly: that one rejects with an AbortError. A rejection
    // here would be caught by the lane loop's own try/catch, logged as a lane
    // error, and answered with an error backoff sleep -- i.e. shutdown would
    // look exactly like a crash in the logs, and the loop would go to sleep
    // again on its way out.
    const controller = new AbortController();
    controller.abort();

    await expect(sleep(1_000, controller.signal)).resolves.toBeUndefined();
  });

  it('leaves no abort listener behind after a normal completion', async () => {
    // One signal, many sleeps -- the lane loop's actual usage. A listener
    // added per call and never removed is an unbounded array on a signal that
    // lives for the whole process.
    const controller = new AbortController();
    for (let i = 0; i < 50; i++) await sleep(1, controller.signal);

    expect(getEventListeners(controller.signal, 'abort')).toHaveLength(0);
  });

  it('leaves no abort listener behind after an aborted wait', async () => {
    // Weaker than the assertion above, and deliberately kept anyway: this one
    // is ALSO satisfied by `addEventListener`'s `{ once: true }`, which
    // removes the listener as it fires. Deleting the explicit
    // `removeEventListener` leaves this green and turns the previous test red
    // (verified). It stays because `{ once: true }` is itself a thing that
    // could be dropped, and then this is what notices.
    const controller = new AbortController();
    const pending = sleep(60_000, controller.signal);
    controller.abort();
    await pending;

    expect(getEventListeners(controller.signal, 'abort')).toHaveLength(0);
  });

  it('clears the pending timer when aborted, so nothing holds the event loop open', async () => {
    const clearSpy = jest.spyOn(global, 'clearTimeout');
    try {
      const controller = new AbortController();
      const pending = sleep(60_000, controller.signal);
      controller.abort();
      await pending;

      expect(clearSpy).toHaveBeenCalled();
    } finally {
      clearSpy.mockRestore();
    }
  });

  it('works with no signal at all', async () => {
    await expect(sleep(1)).resolves.toBeUndefined();
  });
});
