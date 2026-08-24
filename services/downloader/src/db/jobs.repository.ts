import { and, eq, inArray, lt, sql } from 'drizzle-orm';
import type { Executor } from './executor';
import { jobs, type Job } from './schema';

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
