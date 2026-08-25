import { and, count, desc, eq, inArray, lt, notExists, sql } from 'drizzle-orm';
import type { Executor } from './executor';
import { redactor } from '../redaction/redactor';
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
 * Returns false when this checkpoint DID NOT LAND -- either the job is no
 * longer being enumerated (reaped, cancelled, finished) or the write was
 * rejected as stale by the rule below. Both mean the same thing to the
 * caller: stop paging.
 *
 * Including the stale case is the point, and the first version got it wrong.
 * It discarded the straggler's cursor but still answered `true`, which tells
 * a lane that has just been overtaken "still yours, keep going" -- two
 * concurrent request streams against an endpoint that rate-limits by IP, for
 * as long as the straggler survives. That is a live F2 violation produced by
 * the code written to protect the checkpoint. The predicate was already being
 * computed for the SET clause, so it moved into the WHERE as well and the
 * lane's existing `if (!stillOurs) return 'lost'` path now stops the
 * straggler dead.
 *
 * A consequence worth knowing: every checkpoint must carry strictly more
 * items than the last, so the lane must not checkpoint a page that added
 * nothing. It doesn't -- an empty page is either the blocked signal or the
 * end of the profile, and both leave the loop before reaching here.
 *
 * PROGRESS MOVES FORWARD ONLY, and `enumerated_count` -- not the cursor --
 * is what measures it.
 *
 * A lane that was reaped can still be mid-flight when its replacement has
 * already paged past it. That straggler's late checkpoint must not rewind
 * the cursor and make the replacement re-enumerate pages it already has;
 * monotonicity is what makes the handover safe without an ownership token.
 *
 * The first version of this expressed that as `max_cursor = greatest(old,
 * new)` -- monotonic in the cursor's own NUMERIC value. Phase 5 found that
 * to be an assumption about Douyin's paging that Douyin does not honour:
 * `aweme/post` returns `max_cursor` as the create_time (ms) of the OLDEST
 * item in the page, and a profile pages newest-first, so THE CURSOR DROPS ON
 * EVERY PAGE. Under GREATEST the checkpoint freezes at page one's value for
 * the rest of the job while `enumerated_count` keeps climbing and the lease
 * keeps renewing -- healthy in every observable way, and a restart re-pages
 * the profile from page two, spending exactly the F2 request budget the
 * checkpoint exists to protect. No test in this repo could have caught it
 * either: a fake parser is free to hand back ascending numbers.
 *
 * So the cursor is written only when the checkpoint that carries it has seen
 * STRICTLY MORE items than the row already records. `enumerated_count` only
 * ever grows, whichever direction Douyin's cursor moves, so "more items" is
 * "further along" by construction -- and a straggler, which has by
 * definition seen fewer, still cannot rewind anything. Strictly greater, not
 * greater-or-equal: two lanes at the same count have no ordering between
 * them, so the second to arrive must not overwrite the first.
 *
 * If you are about to "simplify" this back to `greatest(max_cursor, ...)`,
 * read the paragraph above first -- and note that the test which proves it
 * has to hand back a DESCENDING cursor to mean anything.
 *
 * THE YARDSTICK ONLY WORKS BECAUSE `enumerated_count` HAS ONE WRITER. This
 * function is the only thing in the codebase that assigns it: `retry`,
 * `cancel`, `reapOrphans` and `finalizeSettled` all deliberately leave it
 * alone. A future writer that reset it -- a phase-7 retry path zeroing
 * progress, say -- would freeze the cursor again in exactly the way the
 * paragraph above describes, from the other end. Add one and this rule needs
 * an ownership token instead.
 */
export async function checkpoint(
  exec: Executor,
  jobId: string,
  input: CheckpointInput,
): Promise<boolean> {
  const updated = await exec
    .update(jobs)
    .set({
      maxCursor: sql`CASE WHEN ${input.enumeratedCount} > ${jobs.enumeratedCount}
        THEN ${input.maxCursor}::bigint
        ELSE ${jobs.maxCursor} END`,
      enumeratedCount: sql`greatest(${jobs.enumeratedCount}, ${input.enumeratedCount})`,
      leaseExpiresAt: leaseFromNow(input.leaseSeconds),
      updatedAt: sql`now()`,
    })
    .where(
      and(
        eq(jobs.id, jobId),
        eq(jobs.status, 'enumerating'),
        // The same predicate the SET clause uses. Here it turns "this write was
        // discarded" into a NO ROWS answer, which is what lets the caller tell a
        // straggler to stop. It also means a rejected checkpoint does not renew
        // the lease -- renewing on the strength of work that was refused would
        // hold the job away from the replacement for another LEASE_SECONDS.
        lt(jobs.enumeratedCount, input.enumeratedCount),
      ),
    )
    .returning({ id: jobs.id });

  return updated.length > 0;
}

/**
 * Phase 5 -- the three ways an enumerate claim ends: finished, blocked, or
 * handed back.
 *
 * All three are conditional UPDATEs gated on `status = 'enumerating'`, and each
 * returns whether it actually wrote a row. That gate is the same one `cancel`
 * and `retry` use, for the same reason: a lane that was reaped mid-flight must
 * not be able to write over a row that now belongs to somebody else. `false`
 * means the claim is gone -- reaped, cancelled, or already finished -- and the
 * caller's business with this job is over.
 *
 * The residual window the gate does NOT close: if the row was reaped AND
 * re-claimed, its status is `enumerating` again and these cannot tell the new
 * claim from the old one. Closing that properly needs an ownership token, which
 * `lane.runner.ts` assigns to phase 6 (the lease re-check before `rename`);
 * the same token belongs here when it lands. Today the window is bounded by
 * arithmetic -- a drain is seconds, a lease is `LEASE_SECONDS` -- and
 * arithmetic is not a guard, which is why it is written down rather than
 * assumed away.
 */

/**
 * Enumerate finished cleanly: the job moves on to the download phase.
 *
 * `total_videos` is counted in SQL from PROFILE state rather than accepted from
 * the lane. A lane adding up page sizes would double-count everything a
 * previous job on the same profile already had -- `videos` is keyed globally on
 * `aweme_id` (F7), so a re-run legitimately re-enumerates rows it is not going
 * to insert. Counting here makes `total_videos` agree with the number of rows
 * that actually exist, which is what every progress reading is measured
 * against.
 *
 * The lease is cleared rather than left to expire: this lane is done with the
 * job, and a stale lease would have the reaper spend a sweep deciding it is not
 * an orphan.
 */
export async function finishEnumerate(exec: Executor, jobId: string): Promise<boolean> {
  const updated = await exec
    .update(jobs)
    .set({
      status: 'downloading',
      totalVideos: sql<number>`(
        select count(*) from ${videos} where ${videos.secUserId} = ${jobs.secUserId}
      )`,
      leaseExpiresAt: null,
      updatedAt: sql`now()`,
    })
    .where(and(eq(jobs.id, jobId), eq(jobs.status, 'enumerating')))
    .returning({ id: jobs.id });

  return updated.length > 0;
}

/**
 * The cookie is being refused: stop, but keep everything needed to resume.
 *
 * `blocked`, NEVER `failed`, and the distinction is the entire remediation
 * signal. Both statuses are retryable (`RETRYABLE_STATUSES` below), so the word
 * itself is what tells an operator WHICH action to take: `blocked` means rotate
 * the cookie and the job will carry on, `failed` means this profile is not
 * gettable and a new cookie will change nothing. Reporting a banned cookie as
 * `failed` -- or a private profile as `blocked` -- sends someone to do the
 * wrong thing, which is why phase 5 only ever calls this on a structural
 * signal and leaves cause-guessing to phase 7's probe (F4).
 *
 * `max_cursor` is deliberately NOT touched. That is what makes the recovery
 * cheap: push a fresh cookie, `/retry`, and enumeration resumes at the
 * checkpoint instead of spending the whole F2 request budget again.
 *
 * The reason is redacted on the way IN, exactly as `videos.repository.ts`'s
 * `markFailed` does it and for the identical reason (plan §0 principle 3): the
 * text phase 7 will pass here comes from Douyin or from a parser error, and
 * scrubbing only in the mapper would leave the raw secret in a column that
 * `psql` and any backup read directly.
 */
export async function markBlocked(
  exec: Executor,
  jobId: string,
  reason: string,
): Promise<boolean> {
  const updated = await exec
    .update(jobs)
    .set({
      status: 'blocked',
      error: redactor.redact(reason),
      leaseExpiresAt: null,
      updatedAt: sql`now()`,
    })
    .where(and(eq(jobs.id, jobId), eq(jobs.status, 'enumerating')))
    .returning({ id: jobs.id });

  return updated.length > 0;
}

/**
 * PHASE 7 (F4) -- the OTHER verdict, and the reason `markBlocked` above is not
 * the only ending an enumerate failure can have.
 *
 * `failed` means "this profile is not gettable and a new cookie will change
 * nothing". It is written on exactly one piece of evidence: a probe of a
 * KNOWN-PUBLIC profile succeeded with the same cookie (`f4-probe.ts`), which
 * rules out every global explanation -- banned cookie, stale `a_bogus`,
 * unreachable parser -- and leaves the target profile itself.
 *
 * Getting the choice between this and `markBlocked` wrong does not break the
 * machine; it sends a person to do the wrong thing. `blocked` on a private
 * profile has an operator rotating a working cookie; `failed` on a banned
 * cookie has them telling a user their profile is gone. That asymmetry is why
 * the probe exists at all (plan §2 F4) and why this function refuses to be a
 * fallback: nothing calls it on a guess.
 *
 * `max_cursor` is deliberately kept, exactly as `markBlocked` keeps it. `failed`
 * is in `RETRYABLE_STATUSES`, so a profile that comes back -- unblocked by its
 * owner, say -- resumes from the checkpoint instead of re-spending the whole F2
 * request budget. A "terminal" status that threw the checkpoint away would make
 * the retry it explicitly permits expensive for no reason.
 *
 * Same `status = 'enumerating'` gate and same redact-on-write as `markBlocked`;
 * see those comments. The reason text originates in a parser error, so plan §0
 * principle 3 applies on the way IN, not merely on the way out through the
 * mapper.
 */
export async function markFailed(
  exec: Executor,
  jobId: string,
  reason: string,
): Promise<boolean> {
  const updated = await exec
    .update(jobs)
    .set({
      status: 'failed',
      error: redactor.redact(reason),
      leaseExpiresAt: null,
      updatedAt: sql`now()`,
    })
    .where(and(eq(jobs.id, jobId), eq(jobs.status, 'enumerating')))
    .returning({ id: jobs.id });

  return updated.length > 0;
}

/**
 * §4.2's "nhả lease" for a job: put it straight back on the queue.
 *
 * Called on graceful shutdown and after a tick that failed, so the next process
 * -- or the next tick -- can claim it immediately rather than idling beside it
 * until the lease expires. The checkpoint is kept, so whoever picks it up
 * resumes from `max_cursor`.
 *
 * `reapOrphans` does the same thing on the timer; this is the fast path for the
 * case where we know we are letting go, and the difference between the two is
 * `LEASE_SECONDS` of a lane doing nothing.
 */
export async function releaseClaim(exec: Executor, jobId: string): Promise<boolean> {
  const updated = await exec
    .update(jobs)
    .set({ status: 'pending', leaseExpiresAt: null, updatedAt: sql`now()` })
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
export interface FinalizeOptions {
  /**
   * Restrict the sweep to one profile -- phase 6 step 8, where the lane that
   * finished a profile's last video finalizes THAT job and nothing else.
   *
   * Omitted, it sweeps every profile, which is the reaper's safety net (see
   * above). One function rather than two because the predicate is the whole
   * substance and two copies of it would drift: the copy nobody reads is
   * always the stale one.
   */
  secUserId?: string;
}

export async function finalizeSettled(
  exec: Executor,
  opts: FinalizeOptions = {},
): Promise<Job[]> {
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
    .where(
      and(
        eq(jobs.status, 'downloading'),
        notExists(outstanding),
        ...(opts.secUserId === undefined ? [] : [eq(jobs.secUserId, opts.secUserId)]),
      ),
    )
    .returning();
}

/**
 * PHASE 6 step 2 -- has the job that wanted this video been cancelled?
 *
 * `videos` has no `job_id` and must never gain one (F7 -- see the long note on
 * the table in schema.ts), so this is answered from PROFILE state, and the
 * shape of the answer matters more than it looks.
 *
 * "Any cancelled job for this profile" is the WRONG reading: one cancel would
 * then silently skip every future job's videos for that profile forever. And
 * "no live job for this profile" on its own is also wrong -- a profile whose
 * only job is `blocked` or `failed` is waiting for /retry, and `skipped` is a
 * status nothing resets (`resetFailedForProfile` touches only `failed`). So it
 * is the CONJUNCTION: somebody cancelled, and nobody else is still asking.
 *
 * RESOLVED IN PHASE 7 (this used to carry a `TODO(phase-7)`): `skipped` was a
 * status nothing reset, so a cancelled profile was un-downloadable forever --
 * and silently, because `skipped` counts as settled, so the next job reported
 * `completed` with zero downloads. BOTH ways back are now implemented, because
 * they answer different intents: `cancelled` joined `RETRYABLE_STATUSES` (retry
 * that job), and `jobs.service.ts` calls `reviveSkippedForProfile` when a new
 * job is created for the profile (somebody wants it again). Either way this
 * function's conjunction then answers `false`, because a live job exists.
 */
export async function isProfileCancelled(exec: Executor, secUserId: string): Promise<boolean> {
  const live = exec
    .select({ one: sql`1` })
    .from(jobs)
    .where(
      and(
        eq(jobs.secUserId, secUserId),
        inArray(jobs.status, ['pending', 'enumerating', 'downloading']),
      ),
    );

  const rows = await exec
    .select({ id: jobs.id })
    .from(jobs)
    .where(
      and(eq(jobs.secUserId, secUserId), eq(jobs.status, 'cancelled'), notExists(live)),
    )
    .limit(1);

  return rows.length > 0;
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

/**
 * States `retry` may act from.
 *
 * phase-03 §3.1 listed `blocked` (stuck on a cookie problem) and `failed` (gave
 * up). Phase 7 adds `cancelled`, and it is a fix rather than a feature: cancel
 * has the download lane mark the profile's outstanding videos `skipped`, and
 * `skipped` was a status nothing in the system reset. So a cancelled profile was
 * permanently un-downloadable -- a new job re-enumerated it, `upsertMany`
 * correctly did nothing (F7), `claimOne` never looks at `skipped`, and because
 * `skipped` counts as settled the new job reported `completed` with zero
 * downloads and no error. See `videos.repository.ts`'s
 * `reviveSkippedForProfile`.
 *
 * `cancelled` is NOT terminal for the same reason `blocked` is not: both are
 * states an operator put the job into and can change their mind about. What
 * stays terminal is `completed` -- there is nothing to retry -- and
 * `enumerating`/`downloading`, which are somebody else's claim.
 *
 * This does not disturb the `blocked` vs `failed` distinction plan §5 anchors on
 * (both were already retryable; the WORD is the remediation signal, and
 * `CANCELLABLE_STATUSES` is the behavioural half).
 */
const RETRYABLE_STATUSES = ['blocked', 'failed', 'cancelled'] as const;

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
