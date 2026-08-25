/**
 * Phase 4 §4.1/§4.2 -- the `LaneRunner` base class.
 *
 * This is the file that has to earn Approach 1 (solution design §4). A1 was
 * chosen over `@Interval` knowing it costs a hand-written lifecycle, and the
 * price named in "trade-offs we accept" is exactly one obligation: CATCH
 * EVERYTHING IN THE LOOP BODY. An exception that escapes the loop does not
 * crash the process, does not fail a healthcheck, and does not alert -- the
 * lane simply stops, and jobs sit at `pending` forever. So the survival tests
 * below are the point of the file, not a formality.
 *
 * Everything here is a pure unit test with NO database and NO Nest container,
 * which is the third reason A1 was chosen (solution design §4): `runOnce()` is
 * callable directly, so each guard can be driven without standing up a
 * scheduler.
 */
import { LaneRunner, type HeldWork } from '../../src/lanes/lane.runner';

/** Silences the runner's own error logging; asserted on where it matters. */
interface MockLogger {
  error: jest.Mock<void, [string]>;
  warn: jest.Mock<void, [string]>;
  log: jest.Mock<void, [string]>;
}
const quietLogger = (): MockLogger => ({
  error: jest.fn(),
  warn: jest.fn(),
  log: jest.fn(),
});

type TickResult = 'worked' | 'idle';

/**
 * Hard cap on how many ticks a scripted lane will keep reporting `'worked'`
 * before it forces an `'idle'`, whatever its script says.
 *
 * This is not tidiness, it is what keeps the FILE observable when the code is
 * wrong. A lane that reports `'worked'` forever with a `runOnce` that never
 * touches I/O starves the macrotask queue -- and `waitFor` below polls on a
 * `setTimeout`, so every test that watches such a lane becomes unobservable at
 * exactly the moment it has something to report. That is not theoretical: the
 * mutation that removes the loop's `setImmediate` yield turned this whole suite
 * into an infinite hang instead of a red test, so the mutation could not be
 * caught at all, and a mutation that cannot be caught is a guard that is not
 * guarded.
 *
 * With the cap, a starved lane eventually goes idle, sleeps on a real timer,
 * the event loop drains, and the waiting assertion FAILS. Red beats hung.
 * Healthy tests reach their targets in tens of ticks and never come near this.
 */
const MAX_SCRIPTED_TICKS = 5_000;

/**
 * A lane whose every tick is scripted by the test.
 *
 * `script` is consulted per tick: a function to call, a value to return, or a
 * value to throw. Anything not scripted falls back to `fallback`.
 */
class ScriptedLane extends LaneRunner {
  readonly name = 'scripted';
  ticks = 0;
  /** Same object as `this.logger`, kept at the mock type so the assertions
   * below can read `.mock.calls` without casting at every use. */
  readonly mockLogger: MockLogger;
  constructor(
    private readonly behaviour: (tick: number) => TickResult | Promise<TickResult>,
    opts: { idleDelayMs?: number; errorBackoffMs?: number; drainTimeoutMs?: number } = {},
  ) {
    const logger = quietLogger();
    super(logger);
    this.mockLogger = logger;
    startedLanes.push(this);
    if (opts.idleDelayMs !== undefined) this.idleDelayMs = opts.idleDelayMs;
    if (opts.errorBackoffMs !== undefined) this.errorBackoffMs = opts.errorBackoffMs;
    if (opts.drainTimeoutMs !== undefined) this.drainTimeoutMs = opts.drainTimeoutMs;
  }
  idleDelayMs = 1;
  errorBackoffMs = 1;
  drainTimeoutMs = 1_000;
  async runOnce(): Promise<TickResult> {
    this.ticks += 1;
    // See MAX_SCRIPTED_TICKS: the escape hatch that keeps a starved loop
    // reportable instead of merely stuck.
    if (this.ticks > MAX_SCRIPTED_TICKS) return 'idle';
    return this.behaviour(this.ticks);
  }
  /** Exposes the protected claim slot to the tests. */
  holdForTest(work: HeldWork): void {
    this.hold(work);
  }
  releaseHoldForTest(): void {
    this.clearHold();
  }
}

/**
 * Every lane this file starts, so `afterEach` can stop it however the test
 * ended. `ScriptedLane`'s constructor registers itself, so there is no way to
 * build one and forget.
 *
 * Not hygiene -- a correctness fix for the harness. The first run of this file
 * HUNG instead of failing: one test's `waitFor` timed out, which threw before
 * that test's own `await lane.stop()`, leaving a loop spinning on a 1ms sleep
 * forever. Jest then had a live timer for the rest of the process and never
 * exited, so an ordinary assertion failure presented as an infinite hang, with
 * no failing test named because the reporter never got to print. A harness
 * that turns a red test into a hang is worse than no harness (plan §5 -- the
 * harness is what grades every other guard, so a bug in it is invisible).
 */
const startedLanes: LaneRunner[] = [];

afterEach(async () => {
  const lanes = startedLanes.splice(0, startedLanes.length);
  // stop() is idempotent, so stopping one a passing test already stopped costs
  // nothing.
  await Promise.all(lanes.map((lane) => lane.stop()));
});

/** Waits until `predicate` is true, or fails the test after `timeoutMs`. */
async function waitFor(predicate: () => boolean, timeoutMs = 3_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error('waitFor timed out');
    await new Promise((r) => setTimeout(r, 5));
  }
}

describe('runOnce() is callable directly, with no scheduler and no container (§4.1)', () => {
  it('a lane can be constructed and ticked by hand', async () => {
    const lane = new ScriptedLane(() => 'idle');

    await expect(lane.runOnce()).resolves.toBe('idle');
    expect(lane.ticks).toBe(1);
    // Nothing was started, so nothing needs stopping -- which is the property
    // that makes every mutation test in this phase cheap to write.
    expect(lane.isRunning).toBe(false);
  });
});

describe('THE guard: an exception in the loop body must not kill the lane (§4.1, plan §5)', () => {
  it('keeps ticking through an exception on every other tick, and lastTickAt keeps advancing', async () => {
    const lane = new ScriptedLane((tick) => {
      if (tick % 2 === 0) throw new Error(`boom on tick ${tick}`);
      return 'idle';
    });

    lane.start();
    await waitFor(() => lane.ticks >= 20);
    const tickAtMidRun = lane.lastTickAt;
    await waitFor(() => lane.ticks >= 40);
    await lane.stop();

    // Survival: ten throws did not end the loop.
    expect(lane.ticks).toBeGreaterThanOrEqual(40);
    // The metric §4.1 asks for kept moving, which is what an alert watches.
    expect(lane.lastTickAt).not.toBeNull();
    expect(lane.lastTickAt!.getTime()).toBeGreaterThanOrEqual(tickAtMidRun!.getTime());
    // And the loop ended because WE stopped it, not because it fell over.
    expect(lane.crashedWith).toBeNull();
  });

  it('survives a thrown value that is not an Error at all', async () => {
    // A `catch (e) { log(e.message) }` reads fine and dies here. Non-Error
    // throws are not hypothetical: a rejected `undici` promise, a `Promise
    // .reject(undefined)`, and a thrown string all reach this catch.
    const thrown: unknown[] = ['a string', null, undefined, 42, { code: 'ENOENT' }];
    const lane = new ScriptedLane((tick) => {
      if (tick <= thrown.length) throw thrown[tick - 1];
      return 'idle';
    });

    lane.start();
    await waitFor(() => lane.ticks >= thrown.length + 5);
    await lane.stop();

    expect(lane.crashedWith).toBeNull();
    expect(lane.ticks).toBeGreaterThan(thrown.length);
  });

  it('survives a runOnce that REJECTS rather than throwing synchronously', async () => {
    const lane = new ScriptedLane(async (tick) => {
      if (tick % 3 === 0) return Promise.reject(new Error('async boom'));
      return 'idle';
    });

    lane.start();
    await waitFor(() => lane.ticks >= 15);
    await lane.stop();

    expect(lane.crashedWith).toBeNull();
  });

  it('logs every exception -- a lane that swallows silently is as bad as one that dies', async () => {
    const lane = new ScriptedLane((tick) => {
      if (tick === 1) throw new Error('the one error');
      return 'idle';
    });

    lane.start();
    await waitFor(() => lane.ticks >= 3);
    await lane.stop();

    expect(lane.mockLogger.error).toHaveBeenCalled();
    const logged = lane.mockLogger.error.mock.calls.flat().join(' ');
    expect(logged).toContain('the one error');
    expect(logged).toContain('scripted');
  });

  it('records where the loop died IF it ever dies, so death is never silent', async () => {
    // The observability half of the guard, deliberately separate from
    // survival: the inner try/catch keeps the lane alive, and `crashedWith` is
    // what makes a lane that fell over anyway visible rather than merely
    // quiet.
    //
    // Reaching the OUTER catch takes some doing, which is the point -- almost
    // nothing is outside the inner one. The only code that runs outside it is
    // the loop's own error handling, so this makes the LOGGER throw: a lane
    // whose tick failed and whose attempt to say so also failed. Rare, and
    // exactly the case where a silent death would be hardest to diagnose.
    const lane = new ScriptedLane(() => {
      throw new Error('tick failed');
    });
    lane.mockLogger.error.mockImplementation(() => {
      throw new Error('the logger itself is broken');
    });

    lane.start();
    await waitFor(() => lane.crashedWith !== null);

    expect(String((lane.crashedWith as Error).message)).toContain('the logger itself is broken');
    // Dead, and honest about being dead -- which is what an operator can act
    // on. `isStale()` will report it within 5 minutes even if nobody reads
    // `crashedWith`.
    expect(lane.isRunning).toBe(true);
    await lane.stop();
    expect(lane.isRunning).toBe(false);
  });
});

describe('pacing (§4.1): idle sleeps, worked does not', () => {
  it('a lane that keeps reporting work still lets the rest of the process run', async () => {
    // Found by this test file HANGING forever the first time it ran.
    //
    // "Do not sleep after a tick that worked" plus a `runOnce` that resolves
    // without touching I/O is a `while` loop whose only yield is to the
    // MICROtask queue. Timers, sockets and signal handlers live in the
    // macrotask queue and never get a turn: the loop starves the event loop
    // outright. In this process that is not abstract -- the API and the lanes
    // share one process by design (plan §Stack) -- so a starving lane means
    // `/health` stops answering, Nest's SIGTERM handler never fires, and the
    // container is SIGKILLed with its lease still held.
    //
    // THE PROBE IS A `setImmediate`, NOT A TIMER, and the loop is capped.
    // Both are deliberate, and both came out of the mutation suite:
    //
    //   - A `setTimeout` probe cannot report starvation, because a starved
    //     event loop also starves the reporting. The first version of this test
    //     used one, and the mutation that removes the yield turned the whole
    //     suite into an infinite hang instead of a red test -- so the mutation
    //     could not be "caught" at all. A `setImmediate` scheduled BEFORE the
    //     loop starts is ahead of the loop's own yield in the check queue, so a
    //     healthy lane lets it fire on the very first tick.
    //   - The tick cap plus the `'idle'` fallback is the escape hatch: once the
    //     lane goes idle it sleeps on a real timer, the event loop drains, and
    //     the assertion below runs and FAILS. A test that hangs when the code
    //     is wrong is not a test.
    const TICK_CAP = 1_000;
    let ticksAtProbe: number | null = null;
    const lane = new ScriptedLane((tick) => (tick < TICK_CAP ? 'worked' : 'idle'), {
      idleDelayMs: 5,
    });
    setImmediate(() => {
      ticksAtProbe = lane.ticks;
    });

    lane.start();
    await waitFor(() => ticksAtProbe !== null);
    await lane.stop();

    // A healthy lane hands the loop a turn every tick, so the probe fires
    // almost immediately. A starved one cannot let it run until the tick cap
    // forces an idle sleep.
    expect(ticksAtProbe).toBeLessThanOrEqual(3);
  });

  it('does NOT sleep after a tick that reported work', async () => {
    // A long idle delay that must never be reached: a lane with work to do
    // has to come straight back for more, or a 500-video download would take
    // 500 x idleDelayMs.
    const lane = new ScriptedLane(() => 'worked', { idleDelayMs: 60_000 });

    lane.start();
    await waitFor(() => lane.ticks >= 25);
    await lane.stop();

    expect(lane.ticks).toBeGreaterThanOrEqual(25);
  });

  it('DOES sleep after a tick that reported idle', async () => {
    const lane = new ScriptedLane(() => 'idle', { idleDelayMs: 60_000 });

    lane.start();
    await waitFor(() => lane.ticks >= 1);
    await new Promise((r) => setTimeout(r, 80));
    const ticksAfterWaiting = lane.ticks;
    await lane.stop();

    // One tick, then it went to sleep for a minute. Without the idle sleep
    // this is a hot loop hammering Postgres with claim queries.
    expect(ticksAfterWaiting).toBe(1);
  });

  it('waits the ERROR backoff after a throw, not the idle delay', async () => {
    // Distinct delays on purpose: a failing lane must back off harder than an
    // idle one. With idleDelayMs at 1ms, a lane that used the idle delay after
    // an error would tick many times in the window below.
    const lane = new ScriptedLane(() => {
      throw new Error('always');
    }, { idleDelayMs: 1, errorBackoffMs: 60_000 });

    lane.start();
    await waitFor(() => lane.ticks >= 1);
    await new Promise((r) => setTimeout(r, 80));
    const ticksAfterWaiting = lane.ticks;
    await lane.stop();

    expect(ticksAfterWaiting).toBe(1);
  });

  it('escalates the backoff over consecutive failures and resets it on success', async () => {
    // A lane failing because Postgres is down must not spend the outage
    // re-asking every errorBackoffMs; and once it recovers it must not stay
    // slow. Observed through the runner's own reported delay rather than by
    // timing, so this test is not a stopwatch race.
    const lane = new ScriptedLane((tick) => {
      if (tick <= 4) throw new Error('down');
      return 'idle';
    }, { errorBackoffMs: 10, idleDelayMs: 1 });

    const delays: number[] = [];
    lane.onBackoff = (ms) => delays.push(ms);

    lane.start();
    await waitFor(() => lane.ticks >= 6);
    await lane.stop();

    expect(delays.length).toBeGreaterThanOrEqual(4);
    expect(delays[1]).toBeGreaterThan(delays[0]);
    expect(delays[2]).toBeGreaterThan(delays[1]);
    // Capped, or a lane that has been failing all night wakes up once a week.
    expect(Math.max(...delays)).toBeLessThanOrEqual(lane.maxBackoffMs);
    // Recovered: the next failure would start from the floor again.
    expect(lane.currentBackoffMs).toBe(lane.errorBackoffMs);
  });
});

describe('start()/stop() lifecycle (§4.2)', () => {
  it('start() twice does not produce two loops', async () => {
    const lane = new ScriptedLane(() => 'worked', { idleDelayMs: 1 });

    lane.start();
    lane.start();
    await waitFor(() => lane.ticks >= 10);
    await lane.stop();

    // Two loops would mean two concurrent claims from one lane -- which is
    // how "concurrency is the number of loops" (solution design §4, the
    // structural guarantee behind F2 and DOWNLOAD_CONCURRENCY) quietly stops
    // being true.
    expect(lane.isRunning).toBe(false);
    const ticksAfterStop = lane.ticks;
    await new Promise((r) => setTimeout(r, 50));
    expect(lane.ticks).toBe(ticksAfterStop);
  });

  it('stop() returns promptly even when the lane is mid-idle-sleep', async () => {
    const lane = new ScriptedLane(() => 'idle', { idleDelayMs: 60_000 });

    lane.start();
    await waitFor(() => lane.ticks >= 1);
    const started = Date.now();
    await lane.stop();

    // The reaper's real idle delay IS 60s. If stop() waited it out, `docker
    // stop` would SIGKILL us at 10s and the lease-release step would never run.
    expect(Date.now() - started).toBeLessThan(1_000);
    expect(lane.isRunning).toBe(false);
  });

  it('stop() before start() is a no-op, not a hang', async () => {
    const lane = new ScriptedLane(() => 'idle');
    await expect(lane.stop()).resolves.toBeUndefined();
  });

  it('stop() twice is safe', async () => {
    const lane = new ScriptedLane(() => 'idle', { idleDelayMs: 1 });
    lane.start();
    await waitFor(() => lane.ticks >= 2);
    await lane.stop();
    await expect(lane.stop()).resolves.toBeUndefined();
  });

  it('stops claiming new work as soon as stop() is called (§4.2 "ngừng claim việc mới")', async () => {
    let ticksSeenAfterAbort = 0;
    const lane = new ScriptedLane(function (this: void) {
      return 'worked';
    }, { idleDelayMs: 1 });
    lane.start();
    await waitFor(() => lane.ticks >= 5);
    const stopping = lane.stop();
    const ticksAtStop = lane.ticks;
    await stopping;
    ticksSeenAfterAbort = lane.ticks - ticksAtStop;

    // At most the one tick already in flight when abort() landed.
    expect(ticksSeenAfterAbort).toBeLessThanOrEqual(1);
  });
});

describe('graceful shutdown releases held work (§4.2 "nhả lease")', () => {
  it('releases the claim the lane was holding', async () => {
    const release = jest.fn().mockResolvedValue(undefined);
    const lane = new ScriptedLane(() => 'idle', { idleDelayMs: 60_000 });
    lane.start();
    await waitFor(() => lane.ticks >= 1);
    lane.holdForTest({ describe: 'video aweme-1', release });

    await lane.stop();

    // Releasing at shutdown -- rather than letting the reaper find it -- is
    // what makes a restart pick the work up IMMEDIATELY instead of after
    // LEASE_SECONDS. That is the §4.2 DoD line, and it is the reason this
    // step exists at all given the reaper would eventually get there.
    expect(release).toHaveBeenCalledTimes(1);
  });

  it('does not release work the lane already finished', async () => {
    const release = jest.fn().mockResolvedValue(undefined);
    const lane = new ScriptedLane(() => 'idle', { idleDelayMs: 60_000 });
    lane.start();
    await waitFor(() => lane.ticks >= 1);
    lane.holdForTest({ describe: 'video aweme-1', release });
    lane.releaseHoldForTest();

    await lane.stop();

    // A lane that completed its download and cleared the slot must not have
    // its finished row handed back to `pending` on the way out.
    expect(release).not.toHaveBeenCalled();
  });

  it('releases exactly once even if stop() is called twice', async () => {
    const release = jest.fn().mockResolvedValue(undefined);
    const lane = new ScriptedLane(() => 'idle', { idleDelayMs: 60_000 });
    lane.start();
    await waitFor(() => lane.ticks >= 1);
    lane.holdForTest({ describe: 'video aweme-1', release });

    await lane.stop();
    await lane.stop();

    // A second release would stomp a row a DIFFERENT worker may have claimed
    // by then, handing live work back to `pending` underneath it.
    expect(release).toHaveBeenCalledTimes(1);
  });

  it('releases AGAIN after a stop/start/stop cycle', async () => {
    // Found by reviewing `releaseHeld`'s memoisation, not by a failing test.
    //
    // `releasing` exists so two concurrent `stop()` calls cannot both release
    // -- a second release could hand back a row a DIFFERENT worker has since
    // claimed. But it was memoised for the LANE'S LIFETIME, not for the claim:
    // once a lane had released anything, `releasing` stayed truthy forever, so
    // a restarted lane's next claim was never handed back. Nothing restarts a
    // lane in one process today, which is exactly why this would have sat there
    // until something did.
    const first = jest.fn().mockResolvedValue(undefined);
    const second = jest.fn().mockResolvedValue(undefined);
    const lane = new ScriptedLane(() => 'idle', { idleDelayMs: 60_000 });

    lane.start();
    await waitFor(() => lane.ticks >= 1);
    lane.holdForTest({ describe: 'video one', release: first });
    await lane.stop();

    lane.start();
    await waitFor(() => lane.ticks >= 2);
    lane.holdForTest({ describe: 'video two', release: second });
    await lane.stop();

    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });

  it('still releases when the release call itself throws, and logs it', async () => {
    const lane = new ScriptedLane(() => 'idle', { idleDelayMs: 60_000 });
    lane.start();
    await waitFor(() => lane.ticks >= 1);
    lane.holdForTest({
      describe: 'video aweme-1',
      release: () => Promise.reject(new Error('postgres went away')),
    });

    // Shutdown must not become "the process hangs / throws on the way out"
    // just because the database is the thing that died.
    await expect(lane.stop()).resolves.toBeUndefined();
    const logged = lane.mockLogger.error.mock.calls.flat().join(' ');
    expect(logged).toContain('postgres went away');
  });

  it('releases held work even when the drain times out (hard timeout, §4.2)', async () => {
    const release = jest.fn().mockResolvedValue(undefined);
    // A tick that never finishes: a stalled CDN socket with no timeout, which
    // is precisely why §4.2 says the drain wait is bounded.
    const lane = new ScriptedLane(() => new Promise<TickResult>(() => {}), {
      drainTimeoutMs: 50,
    });
    lane.start();
    await waitFor(() => lane.ticks >= 1);
    lane.holdForTest({ describe: 'video aweme-1', release });

    const started = Date.now();
    await lane.stop();

    expect(Date.now() - started).toBeLessThan(2_000);
    expect(release).toHaveBeenCalledTimes(1);
    const logged = lane.mockLogger.warn.mock.calls.flat().join(' ');
    expect(logged).toMatch(/drain/i);
  });

  it('waits for an in-flight tick to finish before releasing (drain, not abandon)', async () => {
    let finished = false;
    let releasedBeforeFinish = false;
    const lane = new ScriptedLane(
      () =>
        new Promise<TickResult>((resolve) =>
          setTimeout(() => {
            finished = true;
            resolve('worked');
          }, 120),
        ),
      { drainTimeoutMs: 5_000 },
    );
    lane.start();
    await waitFor(() => lane.ticks >= 1);
    lane.holdForTest({
      describe: 'video aweme-1',
      release: async () => {
        releasedBeforeFinish = !finished;
      },
    });

    await lane.stop();

    // Releasing the lease while the tick is still writing bytes is the
    // two-writers-one-file race in the risk register, arrived at from the
    // inside.
    expect(finished).toBe(true);
    expect(releasedBeforeFinish).toBe(false);
  });
});

describe('lastTickAt (§4.1 metric)', () => {
  it('is null before the lane has ever ticked', () => {
    const lane = new ScriptedLane(() => 'idle');
    expect(lane.lastTickAt).toBeNull();
  });

  it('advances on a tick that did work AND on a tick that was idle', async () => {
    const lane = new ScriptedLane((tick) => (tick % 2 === 0 ? 'worked' : 'idle'), {
      idleDelayMs: 1,
    });
    lane.start();
    await waitFor(() => lane.ticks >= 1);
    const first = lane.lastTickAt;
    await waitFor(() => lane.lastTickAt !== first);
    await lane.stop();

    expect(lane.lastTickAt).not.toBeNull();
  });

  it('reports staleness, which is what a 5-minute alert reads (§4.1)', async () => {
    const lane = new ScriptedLane(() => 'idle');
    // Never STARTED: reportable straight away. A lane the supervisor skipped is
    // a real problem and there is nothing to wait for.
    expect(lane.isStale()).toBe(true);

    lane.start();
    await waitFor(() => lane.ticks >= 1);
    await lane.stop();

    expect(lane.isStale()).toBe(false);
    expect(lane.staleAfterMs).toBe(5 * 60_000);
  });

  it('does not call a just-started lane stale before its window has even elapsed', async () => {
    // Found by reading the alert text this would produce, not by a red test.
    //
    // `isStale()` answered true for any lane that had not ticked yet, whatever
    // the clock said. So a lane whose first tick is slow -- an enumerate lane
    // waiting on a cold parser, say -- produced, sixty seconds in, the line
    // "has not ticked in over 300000ms (last tick: never)". That claim is
    // simply false, and a false alert is worse than none: it trains an
    // operator to ignore the real one.
    const lane = new ScriptedLane(() => new Promise<TickResult>(() => {}));
    const startedAt = Date.now();

    lane.start();
    await waitFor(() => lane.ticks >= 1);

    // One minute in, with no tick completed: not yet stale.
    expect(lane.isStale(startedAt + 60_000)).toBe(false);
    // Past the window with still no tick: now it is.
    expect(lane.isStale(startedAt + lane.staleAfterMs + 1)).toBe(true);
  });
});
