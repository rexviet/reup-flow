import { and, count, desc, eq, inArray, lt, notExists, sql } from 'drizzle-orm';
import type { Executor } from './executor';
import { jobs, profiles, videos, type Job } from './schema';

export interface ClaimOptions {
  /** How long a claim is valid before the reaper may hand it to someone else. */
  leaseSeconds: number;
}

export interface CheckpointInput {
  /** Douyin's paging cursor -- a millisecond timestamp. */
  maxCursor: number;
  /** Items enumerated by this run so far. */
  enumeratedCount: number;
  /** Renews the lease for this long, measured from the database clock. */
  leaseSeconds: number;
}

/** Lease expiry, always computed from the DATABASE clock (see schema.ts). */
function leaseFromNow(leaseSeconds: number) {
  return sql`now() + make_interval(secs => ${leaseSeconds})`;
}

/**
 * Claims one pending job for enumeration, oldest first.
 *
 * Same `FOR UPDATE SKIP LOCKED` shape as the video claim, for the same
 * reason -- see videos.repository.ts claimOne() for the full note on why
 * removing it does not obviously break, and what the concurrency test does
 * about that.
 *
 * Note this claims a job to a lane; it does not decide how many lanes may
 * enumerate at once. F2 (global concurrency of exactly 1, because Douyin
 * rate-limits by IP and not by job) is enforced by the enumerate lane in
 * phase 5, not here.
 */
export async function claimOne(exec: Executor, opts: ClaimOptions): Promise<Job | null> {
  const claimable = exec
    .select({ id: jobs.id })
    .from(jobs)
    .where(eq(jobs.status, 'pending'))
    .orderBy(jobs.createdAt)
    .limit(1)
    .for('update', { skipLocked: true });

  const [claimed] = await exec
    .update(jobs)
    .set({
      status: 'enumerating',
      leaseExpiresAt: leaseFromNow(opts.leaseSeconds),
      updatedAt: sql`now()`,
    })
    .where(inArray(jobs.id, claimable))
    .returning();

  return claimed ?? null;
}

/**
 * Writes the enumerate checkpoint and renews the lease in one statement.
 *
 * Checkpointing and lease renewal are the same operation on purpose: a long
 * enumerate that made progress has proved it is alive, and one that has not
 * checkpointed has not. Splitting them would let a wedged lane hold a job
 * indefinitely by renewing a lease it is not earning.
 *
 * Returns false when the job is no longer being enumerated -- it was
 * reaped, cancelled, or finished -- which is the caller's signal to stop
 * rather than keep paging a job it no longer holds.
 *
 * Both counters move FORWARD ONLY. A lane that was reaped can still be
 * mid-flight when its replacement has already paged past it; without
 * GREATEST, that straggler's late checkpoint would rewind the cursor and
 * make the replacement re-enumerate pages it already has. Monotonicity is
 * what makes the handover safe without an ownership token, and a cursor
 * that only ever advances is the correct semantics for paging anyway.
 */
export async function checkpoint(
  exec: Executor,
  jobId: string,
  input: CheckpointInput,
): Promise<boolean> {
  const updated = await exec
    .update(jobs)
    .set({
      maxCursor: sql`greatest(${jobs.maxCursor}, ${input.maxCursor})`,
      enumeratedCount: sql`greatest(${jobs.enumeratedCount}, ${input.enumeratedCount})`,
      leaseExpiresAt: leaseFromNow(input.leaseSeconds),
      updatedAt: sql`now()`,
    })
    .where(and(eq(jobs.id, jobId), eq(jobs.status, 'enumerating')))
    .returning({ id: jobs.id });

  return updated.length > 0;
}

/**
 * F5 -- hands back every job whose lane died mid-enumerate.
 *
 * Without this the job is not merely delayed, it is dead: the lane only
 * ever claims `pending`, so a job stuck in `enumerating` is never looked at
 * again, and it fails with no error and no log line. The job keeps its
 * `max_cursor`, so the lane that picks it up resumes from the checkpoint
 * instead of re-paging the profile from the top -- which matters because
 * re-paging costs requests against an IP-based rate limit.
 */
export async function reapOrphans(exec: Executor): Promise<Job[]> {
  return exec
    .update(jobs)
    .set({ status: 'pending', leaseExpiresAt: null, updatedAt: sql`now()` })
    .where(and(eq(jobs.status, 'enumerating'), lt(jobs.leaseExpiresAt, sql`now()`)))
    .returning();
}

/**
 * Phase 4 §4.3 -- finishes any `downloading` job whose videos have all
 * settled. The reaper's safety net, not the normal path.
 *
 * The normal path is phase 6 step 8: whichever download lane finishes the last
 * video for a profile flips the job to `completed`. That check can lose. Two
 * lanes finishing the profile's last two videos at the same moment can both
 * look, both see one row still outstanding (each other's), and both correctly
 * decline -- so nobody finalizes. The job then sits at `downloading` forever
 * with every video `done`: to an operator a stuck job, to the UI a progress
 * bar frozen at 100%, and to the lane nothing at all, because there is no work
 * left for it to notice. Reaper sweep, 60s, done.
 *
 * `downloading` ONLY. `enumerating` is deliberately excluded and the reason is
 * not symmetry: a job whose lane died mid-enumerate has no videos yet, so
 * "every video has settled" is vacuously true and this would mark it complete
 * having never read a single page. Those jobs belong to `reapOrphans` (F5),
 * which puts them back to `pending` so enumeration resumes from `max_cursor`.
 *
 * The counters are recomputed here from PROFILE state rather than left at
 * whatever the lanes last wrote. A job that reaches `completed` through this
 * path reached it because the normal update did NOT run, so its counters are
 * exactly the ones most likely to be stale -- and "completed, 0 downloaded"
 * is a bug report waiting to happen. Profile-scoped, not job-scoped: see the
 * long comment on `videos` in schema.ts (F7) for why `videos` has no `job_id`
 * and why this is the honest number.
 */
export async function finalizeSettled(exec: Executor): Promise<Job[]> {
  /** Videos for THIS job's profile that still owe work. */
  const outstanding = exec
    .select({ one: sql`1` })
    .from(videos)
    .where(
      and(
        eq(videos.secUserId, jobs.secUserId),
        inArray(videos.status, ['pending', 'downloading']),
      ),
    );

  const countForProfile = (status: string) => sql<number>`(
    select count(*) from ${videos}
     where ${videos.secUserId} = ${jobs.secUserId}
       and ${videos.status} = ${status}
  )`;

  return exec
    .update(jobs)
    .set({
      status: 'completed',
      downloadedCount: countForProfile('done'),
      failedCount: countForProfile('failed'),
      totalVideos: sql<number>`(
        select count(*) from ${videos} where ${videos.secUserId} = ${jobs.secUserId}
      )`,
      updatedAt: sql`now()`,
    })
    .where(and(eq(jobs.status, 'downloading'), notExists(outstanding)))
    .returning();
}

/**
 * Phase 3 (HTTP layer) -- the rest of this file, below, is what
 * `jobs.controller.ts` writes rows through. None of it is on the lane's hot
 * path; it exists so the controller never has to hand-roll SQL, which is
 * what would make the "controller only ever writes a row" invariant (plan §0
 * principle 1) a matter of discipline instead of a matter of what functions
 * exist to call.
 */

/**
 * `insert ... on conflict (sec_user_id) do nothing`.
 *
 * `jobs.sec_user_id` has a FK to `profiles`, so a job on a profile that has
 * never been seen before fails at insert with a foreign-key violation unless
 * the profile row exists first. A second job on an already-known profile is
 * the common case (F7 -- profiles are the whole point of incremental
 * re-runs), so this has to be idempotent rather than a plain INSERT that
 * would throw a unique-violation on every repeat job.
 */
export async function upsertProfile(exec: Executor, secUserId: string): Promise<void> {
  await exec.insert(profiles).values({ secUserId }).onConflictDoNothing();
}

/** Inserts a new `pending` job. Caller (jobs.service.ts) is responsible for
 * having called `upsertProfile` first in the SAME transaction -- see the
 * note there. */
export async function create(exec: Executor, secUserId: string): Promise<Job> {
  const [job] = await exec.insert(jobs).values({ secUserId }).returning();
  // Postgres's own contract for a single-row RETURNING INSERT guarantees
  // exactly one row back; this is defensive against a future refactor that
  // turns this into a batch insert, not a case this can hit today.
  if (!job) throw new Error('insert into jobs returned no row');
  return job;
}

export interface PageOptions {
  limit: number;
  offset: number;
}

export interface Page<T> {
  items: T[];
  total: number;
}

/** Newest first -- what an operator watching the jobs list actually wants,
 * and what `jobs_status_created_idx` covers well enough even unfiltered. */
export async function list(exec: Executor, opts: PageOptions): Promise<Page<Job>> {
  const items = await exec
    .select()
    .from(jobs)
    .orderBy(desc(jobs.createdAt))
    .limit(opts.limit)
    .offset(opts.offset);
  const [row] = await exec.select({ value: count() }).from(jobs);
  return { items, total: Number(row?.value ?? 0) };
}

export async function findById(exec: Executor, id: string): Promise<Job | null> {
  const [row] = await exec.select().from(jobs).where(eq(jobs.id, id));
  return row ?? null;
}

/** States `cancel` may act from. Matches phase-03 §3.1 exactly; a job that
 * has already finished (`completed`/`failed`/`cancelled`) has nothing left
 * to cancel. */
const CANCELLABLE_STATUSES = ['pending', 'enumerating', 'downloading', 'blocked'] as const;

/**
 * Cancels a job, but only out of a non-terminal status.
 *
 * A single conditional UPDATE (status IN (...) in the WHERE clause), not a
 * read-then-write: two requests racing the same job (an operator double-
 * clicking cancel, or a cancel racing a lane's own status change) cannot
 * both observe "still cancellable" and then both write.
 *
 * Returns `null` for BOTH "no such job" and "job exists but is not in a
 * cancellable state" -- the caller (jobs.controller.ts) tells those apart by
 * calling `findById` when this returns null, which is what lets it answer
 * 404 vs 409.
 */
export async function cancel(exec: Executor, id: string): Promise<Job | null> {
  const [row] = await exec
    .update(jobs)
    .set({ status: 'cancelled', updatedAt: sql`now()` })
    .where(and(eq(jobs.id, id), inArray(jobs.status, CANCELLABLE_STATUSES)))
    .returning();
  return row ?? null;
}

/** States `retry` may act from -- see phase-03 §3.1: only a job that got
 * stuck (`blocked` on a cookie problem) or gave up (`failed`) can be retried. */
const RETRYABLE_STATUSES = ['blocked', 'failed'] as const;

/**
 * Resets a stuck or failed job back to `pending` so the enumerate lane picks
 * it up again. Clears `error` (a stale reason would otherwise sit on the job
 * forever, outliving the problem it described) and `lease_expires_at` (this
 * job holds no lease right now; leaving a stale one would let the reaper
 * "recover" a job nobody is enumerating).
 *
 * Same conditional-UPDATE / null-means-either-thing shape as `cancel` above,
 * and for the identical reason -- see that function's comment.
 *
 * Deliberately does NOT reset any video rows; that is
 * `videos.repository.ts`'s `resetFailedForProfile`, called separately by
 * `jobs.service.ts` in the same transaction as this. Two different tables,
 * two different repositories, one caller decides how they compose.
 */
export async function retry(exec: Executor, id: string): Promise<Job | null> {
  const [row] = await exec
    .update(jobs)
    .set({ status: 'pending', error: null, leaseExpiresAt: null, updatedAt: sql`now()` })
    .where(and(eq(jobs.id, id), inArray(jobs.status, RETRYABLE_STATUSES)))
    .returning();
  return row ?? null;
}
