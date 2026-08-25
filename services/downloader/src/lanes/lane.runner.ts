import { sleep } from './sleep';

/**
 * Just enough of a logger for a lane. `LaneRunner` takes this rather than a
 * Nest `LoggerService` so a lane can be constructed and ticked in a plain
 * unit test with no container -- the third reason Approach 1 was chosen
 * (solution design §4).
 *
 * The concrete logger a lane gets in production is `RedactingLogger`, so
 * anything a lane logs is scrubbed by the layer plan §0.3 requires. A lane
 * must NOT reach for `console.log` instead; the stdio patch would catch it,
 * but the log would bypass Nest's context/level handling.
 */
export interface LaneLogger {
  error(message: string): void;
  warn(message: string): void;
  log(message: string): void;
}

/**
 * A claim the lane is holding right now, and how to give it back.
 *
 * §4.2's "nhả lease": on `SIGTERM` the lane stops claiming, drains the tick
 * in flight, and hands back whatever it still holds BEFORE the process exits.
 * The reaper would eventually recover it anyway (F3/F5), but only after the
 * lease expires -- so without this step every restart costs a full
 * `LEASE_SECONDS` of a lane sitting idle next to work it is not allowed to
 * touch. That is the difference this exists to make, and the DoD line
 * ("restart picks it up IMMEDIATELY, not in 5 minutes") is measuring exactly
 * it.
 *
 * NOTE FOR PHASES 5 AND 6 -- `release` must be conditional on still holding
 * the claim. This is called after the drain, and the drain can time out
 * (`drainTimeoutMs`), so in the worst case it runs while a tick is still in
 * flight. An unconditional `UPDATE ... SET status = 'pending'` would then be
 * able to hand back a row a DIFFERENT worker has since claimed. Today that
 * window is closed by arithmetic -- the drain timeout is seconds and a
 * reaper cannot touch the row until `LEASE_SECONDS` (300) have passed -- but
 * arithmetic is not a guard. Phase 6 already owes a lease check immediately
 * before its `rename` (risk register); the same check belongs here.
 */
export interface HeldWork {
  /** For the log line. MUST NOT contain a secret -- it is written out as-is. */
  describe: string;
  release(): Promise<void>;
}

/** What one tick of a lane reports back, which is what sets the pace. */
export type TickOutcome = 'worked' | 'idle';

/**
 * A lane: one long-lived, self-scheduling async loop.
 *
 * This is Approach 1 from solution design §2, chosen in §4 for three reasons
 * that all show up in this file:
 *
 *   1. The pace lives INSIDE the loop. `sleep(3-5s + jitter)` between pages of
 *      one job (F2) is a plain `await` in the subclass's `runOnce`, not a
 *      scheduler interval one layer up where it cannot express "between pages".
 *   2. Concurrency is the NUMBER OF LOOPS. Six download lanes are six
 *      instances; there is no semaphore to miscount, and F2's "exactly one
 *      enumerate lane, globally" is a fact about the provider list rather than
 *      a runtime check.
 *   3. `runOnce()` is public and directly callable, so every guard in phases
 *      4-7 has a mutation test that needs no scheduler.
 *
 * And the price, named in §4's "trade-offs we accept":
 *
 *   THE LOOP BODY CATCHES EVERYTHING.
 *
 * An exception that escapes the loop does not crash the process, does not
 * fail the container healthcheck, and does not alert. The lane just stops, and
 * every job it was responsible for sits at `pending` forever with no error
 * anywhere. That failure mode is the whole reason plan §5 lists a mutation for
 * this class: remove the `try/catch` in `runLoop` and the survival tests in
 * lane.runner.spec.ts must go red.
 */
export abstract class LaneRunner {
  /** Identifies the lane in logs and in the staleness watchdog. */
  abstract readonly name: string;

  /**
   * How long to wait after a tick that found NOTHING to do. Not applied after
   * a tick that worked: a lane with a queue in front of it must come straight
   * back, or a 500-video profile takes 500 x this.
   */
  abstract readonly idleDelayMs: number;

  /**
   * Floor of the backoff after a FAILED tick, doubling per consecutive
   * failure up to `maxBackoffMs` and resetting on the first success.
   *
   * Separate from `idleDelayMs` because the two situations are not alike. An
   * idle lane is healthy and should stay responsive. A failing lane is very
   * often failing because the thing it depends on is down -- Postgres
   * restarting, the parser wedged -- and re-asking every second adds load to
   * an outage. Escalating means a lane that fails all night is not still
   * hammering at dawn; the cap means it is not asleep for a week either.
   */
  errorBackoffMs = 1_000;
  maxBackoffMs = 60_000;

  /**
   * Hard bound on how long `stop()` waits for the tick in flight (§4.2
   * "timeout cứng"). A stalled socket with no timeout of its own would
   * otherwise hold shutdown open until `docker stop` gives up and `SIGKILL`s,
   * which skips the release step entirely -- the opposite of what waiting was
   * for. 15s sits under Docker's 10s default only in the sense that the
   * release still runs on timeout; see `stop()`.
   */
  drainTimeoutMs = 15_000;

  /** §4.1's metric: an alert fires when a lane has not ticked for this long. */
  readonly staleAfterMs = 5 * 60_000;

  /** Test seam for the backoff-escalation assertion. Never set in production. */
  onBackoff?: (ms: number) => void;

  private aborter: AbortController | null = null;
  private loop: Promise<void> | null = null;
  private held: HeldWork | null = null;
  private tickAt: Date | null = null;
  private crashed: unknown = null;
  private consecutiveFailures = 0;
  /** When `start()` last ran. Null while the lane has never been started. */
  private startedAt: number | null = null;
  private releasing: Promise<void> | null = null;

  constructor(readonly logger: LaneLogger) {}

  /**
   * One unit of this lane's work. Returns `'worked'` if it did something and
   * should be asked again immediately, `'idle'` if there was nothing to do.
   *
   * Public, and callable with no loop running -- see reason 3 above. `signal`
   * is aborted at shutdown; a long tick should check it and give up early
   * rather than rely on the drain timeout.
   */
  abstract runOnce(signal: AbortSignal): Promise<TickOutcome>;

  /** When the last tick COMPLETED, successfully or not. Null until the first. */
  get lastTickAt(): Date | null {
    return this.tickAt;
  }

  /** Non-null only if the loop itself fell over -- see `runLoop`'s outer catch. */
  get crashedWith(): unknown {
    return this.crashed;
  }

  get isRunning(): boolean {
    return this.loop !== null;
  }

  /**
   * How long the NEXT failure would wait. Derived from the failure count, not
   * stored.
   *
   * Storing it was the first version, as `private backoffMs = this
   * .errorBackoffMs`, and it was wrong in a way no reading catches: a field
   * initialiser runs during the BASE constructor, so it captured the base
   * class's default (1000ms) and a subclass -- or a test -- that set
   * `errorBackoffMs` afterwards was silently ignored. Deriving it reads the
   * current value every time and removes the initialisation-order question
   * altogether.
   */
  get currentBackoffMs(): number {
    return Math.min(this.errorBackoffMs * 2 ** this.consecutiveFailures, this.maxBackoffMs);
  }

  /**
   * True when this lane has not ticked within `staleAfterMs` -- §4.1's
   * "alert if a lane has not ticked for 5 minutes".
   *
   * Three cases, and the middle one is the one that needed thinking about:
   *
   *   never STARTED  -> stale immediately. A lane the supervisor skipped is a
   *                     real problem and there is nothing to wait for.
   *   started, no tick yet -> stale only once the window has actually elapsed.
   *                     An earlier version answered true the instant it was
   *                     asked, which produced the line "has not ticked in over
   *                     300000ms (last tick: never)" sixty seconds after boot
   *                     for any lane with a slow first tick -- an enumerate
   *                     lane waiting on a cold parser, say. That claim is
   *                     false, and a false alert is worse than none because it
   *                     teaches an operator to ignore the real one.
   *   ticked         -> the ordinary comparison.
   */
  isStale(now: number = Date.now()): boolean {
    if (this.startedAt === null) return true;
    const since = this.tickAt?.getTime() ?? this.startedAt;
    return now - since > this.staleAfterMs;
  }

  /** Starts the loop. Idempotent: a second call is ignored. */
  start(): void {
    // Two loops in one lane would break the property that makes A1's
    // concurrency control structural -- "N loops" stops meaning "at most N
    // concurrent claims" (solution design §4, F2, DOWNLOAD_CONCURRENCY).
    if (this.loop) return;
    this.aborter = new AbortController();
    this.crashed = null;
    this.startedAt = Date.now();
    this.loop = this.runLoop(this.aborter.signal);
  }

  /**
   * §4.2: stop claiming -> drain what is in flight (bounded) -> release the
   * lease -> return. In that order, and the order is the point.
   */
  async stop(): Promise<void> {
    // abort() FIRST, before any awaiting: it is what makes the loop exit at
    // its next boundary instead of claiming more work, and it is what cuts
    // short a sleep that could be 60s long (see sleep.ts).
    this.aborter?.abort();

    const loop = this.loop;
    if (loop) {
      const drained = await withTimeout(loop, this.drainTimeoutMs);
      if (!drained) {
        this.logger.warn(
          `lane ${this.name}: drain timed out after ${this.drainTimeoutMs}ms; ` +
            `releasing held work anyway and exiting`,
        );
      }
    }
    this.loop = null;
    this.aborter = null;

    await this.releaseHeld();
  }

  /** Records a claim so shutdown can hand it back. Called by the subclass. */
  protected hold(work: HeldWork): void {
    this.held = work;
    // Clearing `releasing` here is what scopes the release-at-most-once
    // memoisation to THIS CLAIM rather than to the lane's lifetime. Without
    // it, a lane that had ever released anything kept a resolved promise in
    // `releasing` forever, so its next claim was silently never handed back --
    // and nothing restarts a lane inside one process today, which is precisely
    // why that would have waited for whatever first did.
    this.releasing = null;
  }

  /** Forgets a claim the lane finished with, so shutdown does not undo it. */
  protected clearHold(): void {
    this.held = null;
  }

  /**
   * The loop. Everything about this method is about not dying.
   *
   * The inner `try/catch` is THE guard of this phase. It wraps the whole body
   * -- not just `runOnce` -- so that a throw from the pacing code, the metric
   * write, or the sleep is caught too. `catch (err: unknown)` and formatting
   * through `describeError` rather than `err.message`, because a thrown
   * string, `null`, or a cross-realm Error would otherwise turn the handler
   * itself into the thing that kills the lane.
   *
   * The outer `try/catch` does NOT keep the lane alive -- by the time it runs
   * the loop is over. It exists so that a death is RECORDED (`crashedWith`)
   * instead of vanishing into an unhandled rejection. The two are different
   * invariants and are tested separately.
   */
  private async runLoop(signal: AbortSignal): Promise<void> {
    try {
      while (!signal.aborted) {
        try {
          const outcome = await this.runOnce(signal);
          this.tickAt = new Date();
          this.consecutiveFailures = 0;
          if (outcome === 'idle') await sleep(this.idleDelayMs, signal);
          // A tick that worked comes straight back for more -- but not without
          // handing the event loop a turn first.
          //
          // Awaiting only promises that resolve within the microtask queue is
          // a loop the macrotask queue never sees: timers, sockets and signal
          // handlers all starve. That is not hypothetical here, because the
          // API and the lanes share ONE process by design (plan §Stack): a
          // starving lane means `/health` stops answering, Nest's SIGTERM
          // handler never runs, and the container gets SIGKILLed with its
          // lease still held -- the exact outcome §4.2 exists to prevent.
          //
          // Real ticks await Postgres and yield on their own, which is what
          // makes this the kind of bug that surfaces once, under some cheap
          // in-memory path, in production. One `setImmediate` per claim costs
          // nothing next to the round trip that claim just made.
          else await yieldToEventLoop();
        } catch (err: unknown) {
          // The tick still counts as a tick: `lastTickAt` answers "is this
          // lane alive", not "is this lane succeeding". A lane failing every
          // second is alive and must not also trip the staleness alert, which
          // would say the wrong thing about a very specific, wrong problem.
          this.tickAt = new Date();
          this.logger.error(`lane ${this.name} tick failed: ${describeError(err)}`);
          const waitMs = this.currentBackoffMs;
          this.onBackoff?.(waitMs);
          this.consecutiveFailures += 1;
          await sleep(waitMs, signal);
        }
      }
    } catch (err: unknown) {
      // Record FIRST, then try to say so. If the logger is the thing that
      // broke -- which is how this catch is reached at all, since the only
      // code outside the inner try is the inner catch's own error handling --
      // then logging here throws too, the loop promise rejects, and the death
      // becomes an unhandled rejection instead of a recorded fact. The last
      // line of defence must not be able to fall over. (Found by the
      // crashedWith test doing exactly this.)
      this.crashed = err;
      try {
        this.logger.error(`lane ${this.name} LOOP DIED: ${describeError(err)}`);
      } catch {
        // Nothing left to try. `crashedWith` and `isStale()` are what report
        // this now.
      }
    }
  }

  /**
   * Hands back the claim, at most once, and never by throwing.
   *
   * `releasing` memoises the attempt so two `stop()` calls (Nest's shutdown
   * hook firing alongside an explicit close, say) cannot both release. A
   * second release is not harmless: by then the row may belong to another
   * worker, and handing it back to `pending` underneath it is the
   * two-workers-one-file race arrived at from a different direction. The
   * memoisation is reset by `hold()`, so it bounds releases PER CLAIM and not
   * per lane -- see the note there.
   *
   * A failure here is logged and swallowed. The thing that most plausibly
   * makes `release()` throw is Postgres being gone -- and in that case there
   * is nothing to release into, the lease will expire on its own, and the
   * reaper will pick the row up on the next start. Rethrowing would turn a
   * clean exit into a crash on the way out for no gain.
   */
  private async releaseHeld(): Promise<void> {
    if (!this.held) return;
    if (this.releasing) return this.releasing;
    const work = this.held;
    this.releasing = (async () => {
      try {
        await work.release();
        this.logger.log(`lane ${this.name}: released ${work.describe} on shutdown`);
      } catch (err: unknown) {
        this.logger.error(
          `lane ${this.name}: failed to release ${work.describe} on shutdown: ${describeError(err)}`,
        );
      } finally {
        this.held = null;
      }
    })();
    return this.releasing;
  }
}

/**
 * Resolves `true` if `promise` settled in time, `false` if the timeout won.
 *
 * Never rejects and never leaves a timer behind: the loop promise it is
 * handed cannot reject (`runLoop` catches everything), but a dangling timer
 * would keep the process alive past the exit this exists to make prompt.
 */
async function withTimeout(promise: Promise<void>, ms: number): Promise<boolean> {
  let timer: NodeJS.Timeout | undefined;
  const timedOut = new Promise<false>((resolve) => {
    timer = setTimeout(() => resolve(false), ms);
  });
  try {
    return await Promise.race([promise.then(() => true), timedOut]);
  } finally {
    clearTimeout(timer);
  }
}

/** Hands control back to the macrotask queue exactly once. See its caller. */
function yieldToEventLoop(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

/**
 * Renders anything throwable as a log line.
 *
 * Not `err.message`, and not `err instanceof Error ? ... : ...` either. A
 * thrown string, `null`, or a plain object all reach the loop's catch, and so
 * does an `Error` from another realm -- which `instanceof` reports as a
 * non-Error (this is not theoretical; it is what jest's separate realm did to
 * an earlier version of `sleep.ts`). Duck-typing on `stack`/`message` covers
 * every one of those without the catch handler becoming the thing that kills
 * the lane.
 */
function describeError(err: unknown): string {
  if (err === null) return 'null';
  if (err === undefined) return 'undefined';
  if (typeof err === 'object') {
    const maybe = err as { stack?: unknown; message?: unknown };
    if (typeof maybe.stack === 'string') return maybe.stack;
    if (typeof maybe.message === 'string') return maybe.message;
    try {
      return JSON.stringify(err);
    } catch {
      return Object.prototype.toString.call(err);
    }
  }
  return String(err);
}
