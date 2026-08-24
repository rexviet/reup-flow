import { and, eq, inArray, lt, sql } from 'drizzle-orm';
import type { Executor } from './executor';
import { videos, type Video } from './schema';

export interface ClaimOptions {
  /** How long a claim is valid before the reaper may hand it to someone else. */
  leaseSeconds: number;
  /** Attempts a single video gets before it is given up on for good. */
  maxAttempts: number;
}

export type GiveBackOptions = Pick<ClaimOptions, 'maxAttempts'>;

/**
 * Where a claimed video goes when it is handed back.
 *
 * Every give-back path -- voluntary release, reported failure, reaped
 * orphan -- routes through this, because they all face the same question
 * and must answer it identically. Handing a row back to `pending` when its
 * attempts are spent would strand it: the claim query filters on
 * `attempts < maxAttempts`, so nothing would ever pick it up again, while
 * every progress query kept counting it as outstanding work. `failed` is
 * both the honest state and the one `/retry` (phase 7) knows how to reset.
 *
 * Expressed as a CASE inside the UPDATE rather than a read-then-write, so
 * two lanes racing the same row cannot both read `attempts` before either
 * writes.
 */
function giveBackStatus(maxAttempts: number) {
  return sql`CASE WHEN ${videos.attempts} >= ${maxAttempts} THEN 'failed' ELSE 'pending' END`;
}

/**
 * Claims one video for download, oldest first.
 *
 * Shape (verified end-to-end by the phase-0 gate, plan §3.1):
 *
 *   UPDATE videos SET ... WHERE aweme_id IN (
 *     SELECT aweme_id FROM videos
 *      WHERE status = 'pending' AND attempts < :max
 *      ORDER BY create_time LIMIT 1
 *        FOR UPDATE SKIP LOCKED
 *   ) RETURNING *
 *
 * `SKIP LOCKED` is what makes N workers safe on one table: a worker racing
 * a row another worker already locked steps over it and takes the next one,
 * instead of blocking behind it. Removing it does NOT obviously break --
 * Postgres's LockRows node re-tries the next candidate once the blocking
 * transaction commits, so a fast-committing test suite can still come out
 * with the right answer. That is why the concurrency test holds every
 * winning claim open until all contenders settle; see
 * test/support/concurrency-harness.ts.
 *
 * F6 -- `attempts` is incremented HERE, at claim time, not when a download
 * reports failure. A worker that is killed mid-download reports nothing at
 * all, so failure-time counting never fires for exactly the case that needs
 * bounding, and the video is re-claimed forever. The tradeoff, deliberately
 * accepted: three crashes drop a video to `failed` even though it was never
 * really attempted, and an operator has to hit `/retry` to reset it.
 * Stopping is the safer direction than spinning.
 */
export async function claimOne(exec: Executor, opts: ClaimOptions): Promise<Video | null> {
  const claimable = exec
    .select({ awemeId: videos.awemeId })
    .from(videos)
    .where(and(eq(videos.status, 'pending'), lt(videos.attempts, opts.maxAttempts)))
    .orderBy(videos.createTime)
    .limit(1)
    .for('update', { skipLocked: true });

  const [claimed] = await exec
    .update(videos)
    .set({
      status: 'downloading',
      // Database clock, not the app's: see the note on `tstz` in schema.ts.
      leaseExpiresAt: sql`now() + make_interval(secs => ${opts.leaseSeconds})`,
      attempts: sql`${videos.attempts} + 1`,
    })
    .where(inArray(videos.awemeId, claimable))
    .returning();

  return claimed ?? null;
}

/**
 * Commits a finished download. The only path to `done`.
 *
 * Deliberately unconditional -- it does NOT check that the caller still
 * holds the lease. A worker that was reaped mid-download and finishes
 * anyway can therefore mark a row done that someone else is now
 * downloading. That race is real, and phase 6 is where it is closed, at the
 * only point that can close it properly: the `.tmp` file is named uniquely
 * per claim and the lease is re-checked immediately before the rename, so a
 * stale worker's bytes never become the committed file. Adding a lease
 * check here instead would look like protection while providing none, since
 * a lease being live says nothing about whose lease it is.
 */
export async function markDone(
  exec: Executor,
  awemeId: string,
  storagePath: string,
): Promise<void> {
  await exec
    .update(videos)
    .set({ status: 'done', storagePath, leaseExpiresAt: null })
    .where(eq(videos.awemeId, awemeId));
}

/** Records a reported failure and hands the video back (or gives up on it). */
export async function markFailed(
  exec: Executor,
  awemeId: string,
  error: string,
  opts: GiveBackOptions,
): Promise<void> {
  await exec
    .update(videos)
    .set({
      status: giveBackStatus(opts.maxAttempts),
      lastError: error,
      leaseExpiresAt: null,
    })
    .where(eq(videos.awemeId, awemeId));
}

/**
 * Hands a video back without recording an error -- the graceful-shutdown
 * path (phase 4), where the work was simply not finished rather than
 * failed. The attempt taken at claim time is still spent; that is the F6
 * tradeoff, not an oversight.
 */
export async function release(
  exec: Executor,
  awemeId: string,
  opts: GiveBackOptions,
): Promise<void> {
  await exec
    .update(videos)
    .set({ status: giveBackStatus(opts.maxAttempts), leaseExpiresAt: null })
    .where(eq(videos.awemeId, awemeId));
}

/**
 * F3 -- hands back every video whose worker died without releasing it.
 *
 * A worker that is killed mid-download leaves its row in `downloading` with
 * nobody working on it. Nothing else notices: the claim query only looks at
 * `pending`, so the video is simply lost. The expired lease is the only
 * evidence, and this is what acts on it.
 *
 * Expiry is judged by the database clock on both sides of the comparison,
 * so lane hosts with skewed clocks cannot disagree about whether a lease is
 * still live.
 */
export async function reapOrphans(exec: Executor, opts: GiveBackOptions): Promise<Video[]> {
  return exec
    .update(videos)
    .set({
      status: giveBackStatus(opts.maxAttempts),
      leaseExpiresAt: null,
      lastError: sql`CASE WHEN ${videos.attempts} >= ${opts.maxAttempts}
        THEN ${`gave up after ${opts.maxAttempts} attempts: lease expired without completing`}
        ELSE ${videos.lastError} END`,
    })
    .where(and(eq(videos.status, 'downloading'), lt(videos.leaseExpiresAt, sql`now()`)))
    .returning();
}
