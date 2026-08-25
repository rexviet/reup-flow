import { and, count, desc, eq, gt, inArray, isNotNull, lt, notExists, sql } from 'drizzle-orm';
import type { Executor } from './executor';
import { redactor } from '../redaction/redactor';
import { jobs, videos, type Video } from './schema';

export interface ClaimOptions {
  /** How long a claim is valid before the reaper may hand it to someone else. */
  leaseSeconds: number;
  /** Attempts a single video gets before it is given up on for good. */
  maxAttempts: number;
}

export type GiveBackOptions = Pick<ClaimOptions, 'maxAttempts'> & {
  /**
   * PHASE 6 -- the claim token (`attempts` at claim time; see `ownsClaim`).
   *
   * Supplied, the give-back only lands if THIS claim still owns the row. That
   * is the note `lane.runner.ts` leaves for phases 5 and 6: `release` runs
   * after the drain, the drain can time out, so in the worst case it runs while
   * a tick is still in flight -- and an unguarded `UPDATE ... SET status =
   * 'pending'` would then hand back a row a DIFFERENT worker has since claimed.
   * The window used to be closed by arithmetic (a drain is seconds, a lease is
   * 300), and arithmetic is not a guard.
   *
   * Omitted, the write is unconditional, which is what the reaper and the
   * phase-4 shutdown harness want: they act precisely on rows nobody owns.
   */
  attempts?: number;
};

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

/** One row the enumerate lane extracted from an `aweme_list` item. */
export interface VideoUpsert {
  awemeId: string;
  secUserId: string;
  desc: string | null;
  createTime: Date;
  durationMs: number | null;
  /** F1 -- the URL Douyin already attached to the enumerate response. */
  playAddr: string | null;
}

export interface UpsertOptions {
  /** `URL_TTL_SECONDS`. How long the cached `play_addr` is assumed good for. */
  urlTtlSeconds: number;
}

/**
 * The enumerate lane's only write to this table: one page of a profile,
 * inserted, skipping anything already known. Returns how many rows were
 * genuinely NEW.
 *
 * `ON CONFLICT (aweme_id) DO NOTHING`, not `DO UPDATE`, and that is F7's whole
 * mechanism rather than a detail. `aweme_id` is the global key across every job
 * and every run, so a re-run legitimately re-enumerates rows whose files are
 * already on disk. `DO NOTHING` makes that free. `DO UPDATE` would overwrite
 * their `status` and reset `attempts`, re-downloading the entire archive on
 * every job -- destroying exactly the dedupe the key exists to provide.
 *
 * `play_addr` and `url_expires_at` are written HERE, from the enumerate
 * response (F1). That is the saving the whole design is arranged around:
 * resolving a URL per video costs ~525 requests for a 500-video profile against
 * ~25, and Douyin rate-limits by IP. Anything that moved this to download time
 * would put the service back on the path to a ban.
 *
 * `url_expires_at` is computed from `now()` in Postgres, never from
 * `Date.now()`, for the same reason every lease in this schema is (see the
 * `tstz` note in schema.ts): two lane hosts with a few seconds of skew must not
 * disagree about whether a cached URL is still usable.
 *
 * `DO NOTHING` also tolerates a duplicate WITHIN one statement -- two rows with
 * the same `aweme_id` in a single page insert once and return one row. That is
 * measured, not assumed (Postgres 16, `INSERT ... VALUES ('a',1),('a',2) ON
 * CONFLICT DO NOTHING` returns 1), and it matters because Douyin has been seen
 * to repeat an item across a cursor boundary. It is also the second,
 * independent reason `DO UPDATE` is not an option here: the same input makes
 * `DO UPDATE` raise "ON CONFLICT DO UPDATE command cannot affect row a second
 * time", so switching the clause would turn a repeated item from a non-event
 * into a failed tick.
 *
 * An EMPTY page is the one shape that does need handling: drizzle emits
 * `values ()`, which Postgres rejects. The lane reaches it on the last page of
 * a profile whose item count is an exact multiple of the page size, so it is a
 * routine path rather than a defensive one.
 */
export async function upsertMany(
  exec: Executor,
  rows: VideoUpsert[],
  opts: UpsertOptions,
): Promise<number> {
  if (rows.length === 0) return 0;

  const inserted = await exec
    .insert(videos)
    .values(
      rows.map((row) => ({
        ...row,
        urlExpiresAt: sql`now() + make_interval(secs => ${opts.urlTtlSeconds})`,
      })),
    )
    .onConflictDoNothing()
    .returning({ awemeId: videos.awemeId });

  return inserted.length;
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

/**
 * PHASE 6 -- the claim token, and the four statements built on it.
 *
 * The risk register's "Reaper nhả video, 2 worker commit 1 file" needs one
 * thing this schema has no column for: a way for a worker to ask "is this row
 * still MINE?". A live lease is not enough -- `lease_expires_at > now()` is
 * equally true of the REPLACEMENT's lease, which is exactly the case the check
 * exists to catch, and is why `markDone` above refuses to pretend a bare lease
 * check is protection.
 *
 * `attempts` is that token, and it is free. F6 increments it inside the claim
 * UPDATE, so every claim of a row carries a different number and no two live
 * claims can share one. `(aweme_id, attempts)` therefore names a CLAIM rather
 * than a row, and `status = 'downloading' AND attempts = $token AND
 * lease_expires_at > now()` is "my claim, still live" -- judged entirely by the
 * database clock, like every other lease comparison in this file.
 *
 * THE ONE WINDOW IT DOES NOT CLOSE, written down rather than assumed away:
 * `resetFailedForProfile` sets `attempts = 0`, so tokens 1..MAX_ATTEMPTS become
 * available again. A straggler holding token 1 could in principle match a claim
 * issued after such a reset. Getting there takes MAX_ATTEMPTS full lease
 * expiries (25 minutes at the defaults) with the straggler still streaming,
 * plus an operator pressing /retry, plus the replacement claiming inside the
 * straggler's remaining lease. Closing it properly wants a `claim_token uuid`
 * column, which is a migration; this is where that note lives if one is ever
 * added.
 */
function ownsClaim(awemeId: string, attempts: number) {
  return and(
    eq(videos.awemeId, awemeId),
    eq(videos.status, 'downloading'),
    eq(videos.attempts, attempts),
    gt(videos.leaseExpiresAt, sql`now()`),
  );
}

/**
 * Where a give-back may land: this claim, or the row unconditionally.
 *
 * The unconditional form is not a fallback, it is what the reaper and the
 * phase-4 shutdown harness need -- they act on rows whose owner is gone, so
 * there is no live claim to match. See `GiveBackOptions.attempts`.
 */
function giveBackTarget(awemeId: string, opts: GiveBackOptions) {
  return opts.attempts === undefined
    ? eq(videos.awemeId, awemeId)
    : ownsClaim(awemeId, opts.attempts);
}

/**
 * The pre-rename check. True only if this exact claim is still live.
 *
 * Called from inside `Storage.put`, between the last byte and the `rename`
 * (see the `BeforeCommit` note in storage.interface.ts). A `false` here means
 * the reaper handed this video to somebody else while we were downloading it:
 * the bytes are complete, but they must not become the committed file, because
 * the replacement is writing the same key right now and only one rename can be
 * last.
 *
 * A read, not an UPDATE, on purpose. Nothing about deciding "may I commit"
 * should also change the row -- the commit itself is `markDone`, and a check
 * that mutated would make the mutation test for this guard (phase-06.sh) unable
 * to distinguish the check from the commit.
 */
export async function holdsClaim(
  exec: Executor,
  awemeId: string,
  attempts: number,
): Promise<boolean> {
  const rows = await exec
    .select({ awemeId: videos.awemeId })
    .from(videos)
    .where(ownsClaim(awemeId, attempts));
  return rows.length > 0;
}

/**
 * The heartbeat: pushes this claim's lease out by `leaseSeconds` again.
 *
 * A 200MB video on a slow CDN can outlive `LEASE_SECONDS`, and the reaper
 * cannot tell "still downloading" from "worker died" -- both look like a
 * `downloading` row whose lease lapsed. So a live download says so, and the
 * only proof it can offer is that it is still transferring bytes; the lane
 * calls this from the stream's progress, never on a timer alone.
 *
 * Gated on `ownsClaim`, so a worker whose lease ALREADY lapsed cannot renew
 * itself back into ownership: by then the reaper may have handed the row over,
 * and taking it back underneath the replacement is the two-writers race
 * arrived at from the other side. `false` means "you no longer hold this" and
 * the lane stops.
 */
export async function renewLease(
  exec: Executor,
  awemeId: string,
  attempts: number,
  leaseSeconds: number,
): Promise<boolean> {
  const updated = await exec
    .update(videos)
    .set({ leaseExpiresAt: sql`now() + make_interval(secs => ${leaseSeconds})` })
    .where(ownsClaim(awemeId, attempts))
    .returning({ awemeId: videos.awemeId });
  return updated.length > 0;
}

/**
 * F1 -- is the cached `play_addr` still usable?
 *
 * Two conditions, and the second one is not redundant: a row can carry an
 * expiry with no address at all (nothing says Douyin attached a `play_addr` to
 * every `aweme_list` item), and a timestamp-only check would send the lane to
 * GET `null`.
 *
 * Compared in SQL against `now()` rather than in the lane against `Date.now()`.
 * Same rule as every lease in this schema (see the `tstz` note in schema.ts):
 * the expiry was WRITTEN from the database clock, so it has to be READ against
 * the same one, or a host a minute out of step re-resolves URLs that are fine
 * -- spending the request budget F1 exists to protect -- or trusts URLs that
 * are not.
 *
 * One extra round trip per download, deliberately. It is a primary-key lookup
 * against Postgres, next to transferring a video over the internet, and the
 * alternative was widening `claimOne` -- the one query in this file that is
 * EXPLAIN-pinned (`test/db/claim-plan.spec.ts`) and mutation-tested for
 * SKIP LOCKED. Forking it into a second, download-only variant would leave
 * those guards testing a query production no longer runs.
 */
export async function isUrlFresh(exec: Executor, awemeId: string): Promise<boolean> {
  const rows = await exec
    .select({ awemeId: videos.awemeId })
    .from(videos)
    .where(
      and(
        eq(videos.awemeId, awemeId),
        isNotNull(videos.playAddr),
        gt(videos.urlExpiresAt, sql`now()`),
      ),
    );
  return rows.length > 0;
}

/**
 * F1's reactive half: a freshly resolved address, cached with a new TTL.
 *
 * Writes ONLY the two URL columns. It runs mid-download on a row this worker is
 * holding, so touching `status`, `attempts` or the lease would make it a second
 * writer on the claim -- and `attempts` in particular is the claim token, so
 * moving it here would have this function silently invalidate the caller's own
 * commit check.
 *
 * Not gated on `ownsClaim`: caching a URL that Douyin just issued is useful to
 * whoever ends up downloading this video, including a replacement worker, and
 * refusing to write it would make a reaped worker's one useful side effect
 * disappear. The columns it writes are advisory by construction -- the whole
 * point of F1 is that the URL is re-resolved when it stops working.
 */
export async function saveResolvedUrl(
  exec: Executor,
  awemeId: string,
  playAddr: string,
  urlTtlSeconds: number,
): Promise<void> {
  await exec
    .update(videos)
    .set({
      playAddr,
      urlExpiresAt: sql`now() + make_interval(secs => ${urlTtlSeconds})`,
    })
    .where(eq(videos.awemeId, awemeId));
}

/**
 * The job that wanted this video was cancelled: `skipped`, not `failed`.
 *
 * The distinction is the same one `markBlocked` makes for jobs, for the same
 * reason -- the status is what an operator reads to decide whether anything is
 * wrong. `failed` with an error would put a cancelled job's videos in every
 * "what broke?" listing forever, and `/retry` would dutifully bring them back.
 * `last_error` is left null on purpose: nothing went wrong.
 *
 * Gated on `ownsClaim` like every other write this worker makes to its claim.
 */
export async function markSkipped(
  exec: Executor,
  awemeId: string,
  attempts: number,
): Promise<boolean> {
  const updated = await exec
    .update(videos)
    .set({ status: 'skipped', leaseExpiresAt: null })
    // PHASE 7 -- AND NO LIVE JOB WANTS THIS PROFILE, asked in SQL rather than
    // read first.
    //
    // The lane reads `isProfileCancelled` before it claims and again before it
    // commits, but neither read is atomic with THIS write, and the gap is a real
    // sequence with an unrecoverable ending:
    //
    //   1. job A is cancelled.
    //   2. a lane claims video V; the row is now `downloading`.
    //   3. job B is created for the same profile. `reviveSkippedForProfile` runs
    //      inside that transaction and finds nothing: V is `downloading`, not
    //      `skipped`.
    //   4. the lane writes `skipped` here. `ownsClaim` is satisfied, so it lands.
    //
    // V is now `skipped` under a LIVE job. `claimOne` never looks at `skipped`,
    // and `skipped` counts as SETTLED -- so job B finalizes as `completed` with V
    // missing and every counter agreeing. That is phase 6's handover bug rebuilt
    // out of a race, and no status anywhere looks wrong.
    //
    // Making it a predicate closes it without a lock: if a job appeared, this
    // affects no row, `markSkipped` answers false, the lane treats the claim as
    // lost, and the row stays `downloading` for the reaper to hand back (F3).
    // Slower by one lease at worst; the alternative loses the video for good.
    .where(and(ownsClaim(awemeId, attempts), notExists(liveJobForVideoProfile(exec))))
    .returning({ awemeId: videos.awemeId });
  return updated.length > 0;
}

/**
 * "Somebody still wants this profile", correlated to the `videos` row being
 * updated.
 *
 * The positive half of `jobs.repository.ts`'s `isProfileCancelled` conjunction,
 * expressed here so it can be used inside a WHERE. Profile-scoped via the
 * correlated `videos.secUserId`, because `videos` has no `job_id` and must never
 * gain one (F7).
 */
function liveJobForVideoProfile(exec: Executor) {
  return exec
    .select({ one: sql`1` })
    .from(jobs)
    .where(
      and(
        eq(jobs.secUserId, videos.secUserId),
        inArray(jobs.status, ['pending', 'enumerating', 'downloading']),
      ),
    );
}

/**
 * Step 8's question: does this profile still owe work?
 *
 * `pending` or `downloading` -- the two statuses that mean somebody still has
 * to do something. `done`, `failed` and `skipped` have all settled, which is
 * why a job with failures still reaches `completed` rather than hanging (phase
 * 6 §6.3: "đi tiếp, không kill cả job").
 *
 * Profile-scoped because `videos` has no `job_id` and must never gain one
 * (F7 -- see the long note on the table in schema.ts). This is the same
 * predicate `jobs.repository.ts`'s `finalizeSettled` uses as a correlated
 * subquery; the reaper is the safety net for exactly the race this check can
 * lose.
 */
export async function hasWorkForProfile(exec: Executor, secUserId: string): Promise<boolean> {
  const rows = await exec
    .select({ awemeId: videos.awemeId })
    .from(videos)
    .where(
      and(eq(videos.secUserId, secUserId), inArray(videos.status, ['pending', 'downloading'])),
    )
    .limit(1);
  return rows.length > 0;
}

/**
 * Records a reported failure and hands the video back (or gives up on it).
 *
 * `error` is redacted BEFORE it is written, not just before it is read back
 * out through `videos.mapper.ts` -- plan §0 principle 3 and phase-3's own
 * risk register ("cookie vào error của job rồi ra API response"). Phase 6/7
 * will call this with attacker/Douyin-influenced text (a rejected-request
 * message, an upstream error body) this repository never controls, so
 * scrubbing only on the way out would still leave the raw secret sitting in
 * the row -- readable by anyone with `psql`, or by a future export/backup
 * that reads the column directly instead of going through the mapper. See
 * the matching read-side redaction in `videos.mapper.ts`'s `toVideoResponse`
 * for why BOTH sides are scrubbed rather than relying on either alone.
 */
export async function markFailed(
  exec: Executor,
  awemeId: string,
  error: string,
  opts: GiveBackOptions,
): Promise<boolean> {
  const updated = await exec
    .update(videos)
    .set({
      status: giveBackStatus(opts.maxAttempts),
      lastError: redactor.redact(error),
      leaseExpiresAt: null,
    })
    .where(giveBackTarget(awemeId, opts))
    .returning({ awemeId: videos.awemeId });
  return updated.length > 0;
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
): Promise<boolean> {
  const updated = await exec
    .update(videos)
    .set({ status: giveBackStatus(opts.maxAttempts), leaseExpiresAt: null })
    .where(giveBackTarget(awemeId, opts))
    .returning({ awemeId: videos.awemeId });
  return updated.length > 0;
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
  // Redacted through the same call as markFailed even though today's message
  // is a fixed string built only from `opts.maxAttempts` (a number, never a
  // secret): this is the other write path phase-3's risk register names
  // ("Redact phủ cả đường ghi jobs.error" -- and the videos equivalent), and
  // leaving it unguarded would mean the invariant holds only by the CASE
  // branch happening to never carry attacker text yet. A later phase adding
  // upstream detail to this message inherits the guard for free instead of
  // needing to remember it.
  const giveUpMessage = redactor.redact(
    `gave up after ${opts.maxAttempts} attempts: lease expired without completing`,
  );
  return exec
    .update(videos)
    .set({
      status: giveBackStatus(opts.maxAttempts),
      leaseExpiresAt: null,
      lastError: sql`CASE WHEN ${videos.attempts} >= ${opts.maxAttempts}
        THEN ${giveUpMessage}
        ELSE ${videos.lastError} END`,
    })
    .where(and(eq(videos.status, 'downloading'), lt(videos.leaseExpiresAt, sql`now()`)))
    .returning();
}

/**
 * Phase 3 (HTTP layer) -- what `jobs.controller.ts`'s `/retry` route and
 * `videos.controller.ts`'s browse route are built on. Below this point
 * nothing is on the lane's claim/reap hot path.
 */

/**
 * `/retry` support -- resets every `failed` video for a profile back to
 * `pending`, ready to be claimed again.
 *
 * `attempts = 0` is not a cleanup nicety, it is THE reason this function
 * exists rather than a bare `UPDATE videos SET status = 'pending'`. F6 made
 * `claimOne` filter on `attempts < maxAttempts`; a status-only reset produces
 * a row that reads `pending` in every listing and progress count, yet is
 * invisible to the one query that ever picks up pending work. It would sit
 * there forever, looking like outstanding work that no lane will ever touch.
 * If you are about to "simplify" this by dropping the attempts reset,
 * re-read videos.repository.ts's `claimOne` first.
 *
 * `lease_expires_at` is cleared for the same reason `release`/`markFailed`
 * clear it: a video that is about to become `pending` again holds no lease,
 * and a stale one left behind would eventually let the reaper "recover" a
 * video nobody is downloading.
 */
export async function resetFailedForProfile(exec: Executor, secUserId: string): Promise<number> {
  const rows = await exec
    .update(videos)
    .set({ status: 'pending', attempts: 0, lastError: null, leaseExpiresAt: null })
    .where(and(eq(videos.secUserId, secUserId), eq(videos.status, 'failed')))
    .returning({ awemeId: videos.awemeId });
  return rows.length;
}

/**
 * PHASE 7 -- the way back for a CANCELLED job's videos.
 *
 * Separate from `resetFailedForProfile` above, deliberately, and not a widened
 * WHERE on it: `failed` and `skipped` mean opposite things. `failed` is "we
 * tried and could not", so resetting it is a retry. `skipped` is "nothing went
 * wrong, nobody wanted it" (see `markSkipped`), so resetting it is somebody
 * ASKING AGAIN. Folding them together would leave one function whose name is
 * true of half of what it does -- and a `resetFailedForProfile` that quietly
 * un-skips rows is precisely the sort of comment-that-lies plan §5 rule 7 is
 * about.
 *
 * WHY THIS EXISTS AT ALL -- phase 6's handover, and it was a data-loss bug with
 * no guard on it:
 *
 *   cancel -> the lane marks outstanding videos `skipped` -> nothing resets
 *   `skipped` -> a new job re-enumerates and `upsertMany` correctly does nothing
 *   (F7) -> `claimOne` only ever looks at `pending`.
 *
 * So those videos were unreachable forever. And because `skipped` counts as
 * SETTLED (`hasWorkForProfile` ignores it, `finalizeSettled`'s
 * `notExists(outstanding)` is satisfied by it), the next job on that profile
 * walked to `completed` with `downloaded_count = 0` and no error anywhere. Every
 * number agreed; the archive was simply missing the videos. That is the same
 * shape as phase 6's zero-byte commit, and like that one it had no mutation
 * because it had no guard.
 *
 * Called from TWO places, answering two different operator intents, both of
 * which mean "the cancel no longer applies":
 *
 *   - `/retry` on the cancelled job -- "actually, carry on with that".
 *   - creating a NEW job for the profile -- "somebody wants this profile again",
 *     which is also what stops the false `completed` above.
 *
 * `attempts = 0` for the same reason `resetFailedForProfile` does it: the claim
 * filters on `attempts < maxAttempts`, so a row skipped on its last attempt
 * would come back reading `pending` in every listing while staying invisible to
 * the only query that picks up work.
 *
 * `last_error` is NOT cleared, because `markSkipped` never set one -- nothing
 * went wrong. Writing null over null would suggest there had been something to
 * clear.
 */
export async function reviveSkippedForProfile(
  exec: Executor,
  secUserId: string,
): Promise<number> {
  const rows = await exec
    .update(videos)
    .set({ status: 'pending', attempts: 0, leaseExpiresAt: null })
    .where(and(eq(videos.secUserId, secUserId), eq(videos.status, 'skipped')))
    .returning({ awemeId: videos.awemeId });
  return rows.length;
}

export interface ListByProfileOptions {
  limit: number;
  offset: number;
  status?: string;
}

export interface Page<T> {
  items: T[];
  total: number;
}

/**
 * Browse endpoint for one profile's videos, newest first -- exactly the
 * order `videos_profile_recent_idx` was built to serve (schema.ts), so this
 * query is index-only even for a profile with tens of thousands of rows.
 */
export async function listByProfile(
  exec: Executor,
  secUserId: string,
  opts: ListByProfileOptions,
): Promise<Page<Video>> {
  const where = opts.status
    ? and(eq(videos.secUserId, secUserId), eq(videos.status, opts.status))
    : eq(videos.secUserId, secUserId);

  const items = await exec
    .select()
    .from(videos)
    .where(where)
    .orderBy(desc(videos.createTime))
    .limit(opts.limit)
    .offset(opts.offset);
  const [row] = await exec.select({ value: count() }).from(videos).where(where);
  return { items, total: Number(row?.value ?? 0) };
}

/**
 * F7 -- job progress, counted from PROFILE state, not from a per-job delta.
 *
 * `videos` has no `job_id` (see the long comment on the table in schema.ts):
 * `aweme_id` is the global key, so a video downloaded by an earlier job is
 * already `done` before a new job on the same profile ever runs. Grouping by
 * status here, scoped to the profile rather than to any one job, is what
 * makes a job detail response say "340/340 already done" truthfully the
 * instant a second job starts -- that is correct, not a bug, and counting
 * per-job instead would require a join table this schema deliberately does
 * not have.
 */
export async function countByStatusForProfile(
  exec: Executor,
  secUserId: string,
): Promise<Record<string, number>> {
  const rows = await exec
    .select({ status: videos.status, value: count() })
    .from(videos)
    .where(eq(videos.secUserId, secUserId))
    .groupBy(videos.status);
  return Object.fromEntries(rows.map((row) => [row.status, Number(row.value)]));
}
