import type { Executor } from '../db/executor';
import * as jobsRepository from '../db/jobs.repository';
import * as videosRepository from '../db/videos.repository';
import { LaneRunner, type LaneLogger, type TickOutcome } from './lane.runner';
import type { Storage } from '../storage/storage.interface';

/**
 * §4.3: "nhịp 60s". Exported so the lane spec asserts the number rather than
 * trusting it, and so phase 10's alerting has one place to read it from.
 */
export const REAP_INTERVAL_MS = 60_000;

export interface ReaperOptions {
  /** `LEASE_SECONDS`. Sets the give-back threshold and the `.tmp` cutoff. */
  leaseSeconds: number;
  /** `MAX_ATTEMPTS`. A video past it is given up on rather than handed back. */
  maxAttempts: number;
}

/**
 * The reaper: one lane, a fixed 60-second heartbeat, four sweeps.
 *
 * Every sweep here exists because the failure it prevents is SILENT. Nothing
 * crashes, no healthcheck goes red, no log line appears -- work just stops
 * happening, which is the most expensive kind of broken this service can be.
 *
 *   F3        a video stuck in `downloading` is invisible to the claim query,
 *             which only ever looks at `pending`.
 *   F5        the same for a job stuck in `enumerating`. Brainstorm missed
 *             this one entirely; plan §2 flags it as a bug if left to the end,
 *             because without it "kill the worker, watch it resume" is simply
 *             not true of the enumerate phase.
 *   finalize  a job whose videos have all settled but whose status nobody
 *             flipped -- see `jobs.repository.ts`'s `finalizeSettled` for the
 *             race that gets it there.
 *   .tmp      a killed worker's temp file: a full-size video nothing will ever
 *             delete, invisible to `exists()`, eating exactly the space
 *             `freeBytes()` promises the phase-10 preflight (plan §6).
 *
 * The first three recover ROWS; the fourth recovers BYTES. Both halves are
 * needed and only the first three were in the original plan for this lane --
 * the risk register moved the fourth here precisely because the reaper is the
 * only thing that already knows a claim was abandoned.
 */
export class ReaperLane extends LaneRunner {
  readonly name = 'reaper';

  /**
   * A fixed cadence, and `runOnce` always answers `'idle'` to keep it that
   * way -- even on a tick that reclaimed something.
   *
   * Reporting `'worked'` would make the base class come straight back for
   * another pass, and a pass a millisecond later can by definition find
   * nothing: no lease expires in that window. The reaper is a heartbeat, not a
   * queue consumer, so its pace is a constant rather than a function of how
   * much it found.
   */
  readonly idleDelayMs = REAP_INTERVAL_MS;

  constructor(
    logger: LaneLogger,
    private readonly db: Executor,
    private readonly storage: Storage,
    private readonly opts: ReaperOptions,
  ) {
    super(logger);
  }

  /**
   * One sweep of each kind.
   *
   * The four run INDEPENDENTLY and the failures are collected rather than
   * short-circuited, which is this phase's own theme applied one level down.
   * With a plain sequence of `await`s, a first sweep that throws every tick --
   * a permissions problem on DATA_DIR, a lock timeout on one table -- would
   * mean the other three never run again, ever, with nothing to distinguish
   * that from a healthy reaper. Each sweep gets its turn; the tick then fails
   * if any of them did, which is what makes the base class log it and back
   * off.
   *
   * Order is deliberate: rows before bytes. Recovering a row lets work resume;
   * reclaiming a temp file only reclaims space. If a filesystem problem is
   * going to take the tick down, it should do so after the recoveries.
   */
  async runOnce(signal: AbortSignal): Promise<TickOutcome> {
    const failures: unknown[] = [];

    const reapedVideos = await this.attempt(signal, failures, 'videos (F3)', () =>
      videosRepository.reapOrphans(this.db, { maxAttempts: this.opts.maxAttempts }),
    );
    if (reapedVideos?.length) {
      this.logger.warn(
        `reaper: recovered ${reapedVideos.length} orphaned video(s) whose lease expired`,
      );
    }

    const reapedJobs = await this.attempt(signal, failures, 'jobs (F5)', () =>
      jobsRepository.reapOrphans(this.db),
    );
    if (reapedJobs?.length) {
      this.logger.warn(
        `reaper: recovered ${reapedJobs.length} job(s) stranded mid-enumerate; ` +
          `each resumes from its own max_cursor`,
      );
    }

    const finalized = await this.attempt(signal, failures, 'finalize', () =>
      jobsRepository.finalizeSettled(this.db),
    );
    if (finalized?.length) {
      this.logger.log(`reaper: finalized ${finalized.length} job(s) whose videos had all settled`);
    }

    // Cutoff of one full lease. Not an arbitrary number: it is the same
    // evidence the row sweeps above act on, read off the filesystem instead of
    // off Postgres. A worker whose lease has not expired has not been reaped,
    // so its bytes are not garbage -- and deleting a live download's temp file
    // makes its `rename` fail with ENOENT, which phase 2's journal records
    // losing a day to.
    const removed = await this.attempt(signal, failures, 'stale .tmp', () =>
      this.storage.removeStaleTemp(this.opts.leaseSeconds * 1000),
    );
    if (removed?.length) {
      this.logger.warn(`reaper: removed ${removed.length} abandoned .tmp file(s)`);
    }

    if (failures.length > 0) throw asOneError(failures);
    return 'idle';
  }

  /**
   * Runs one sweep, recording rather than propagating its failure.
   *
   * Returns `undefined` on failure -- or when shutdown has begun -- so the
   * caller skips that sweep's logging without having to ask separately whether
   * it ran.
   *
   * The `signal` check is what keeps a shutdown from waiting out three more
   * sweeps it does not need. Nothing already committed is undone by stopping
   * here: every sweep is its own statement, so a partial tick leaves the
   * database consistent and the next tick picks up whatever this one skipped.
   */
  private async attempt<T>(
    signal: AbortSignal,
    failures: unknown[],
    label: string,
    run: () => Promise<T>,
  ): Promise<T | undefined> {
    if (signal.aborted) return undefined;
    try {
      return await run();
    } catch (err: unknown) {
      // Labelled here, where the label is known. By the time the base class's
      // catch sees the aggregate, "which sweep" is exactly the question an
      // operator needs answered and nothing else can answer it.
      failures.push(new Error(`reaper sweep "${label}" failed: ${describe(err)}`));
      return undefined;
    }
  }
}

/**
 * Collapses the tick's failures into one throwable.
 *
 * `AggregateError` is deliberately not used: its own `.message` carries none
 * of the causes' text (the same trap `db.module.ts`'s `describeConnectionError`
 * documents for `pg`), so anything that logs `err.message` -- including the
 * base class -- would print a line saying nothing at all.
 */
function asOneError(failures: unknown[]): Error {
  if (failures.length === 1 && failures[0] instanceof Error) return failures[0];
  return new Error(failures.map(describe).join('; '));
}

function describe(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}
