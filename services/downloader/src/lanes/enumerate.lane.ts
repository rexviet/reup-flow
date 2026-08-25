import type { Executor } from '../db/executor';
import * as jobsRepository from '../db/jobs.repository';
import * as videosRepository from '../db/videos.repository';
import type { Job } from '../db/schema';
import { ParserRejectedError } from '../parser/parser.errors';
import type { UserPostPage } from '../parser/parser.client';
import { toVideoUpsert } from './aweme.mapper';
import { LaneRunner, type LaneLogger, type TickOutcome } from './lane.runner';
import { sleep } from './sleep';

/**
 * Items per page. Douyin's own web client asks for 18; 20 is the count the
 * routes were verified with (plan §1) and keeps a 500-video profile at 25
 * requests.
 */
export const ENUMERATE_PAGE_SIZE = 20;

/**
 * How long to wait before asking for a job again when there was none. Plan
 * §Stack's "polling 1s": the queue is Postgres, and a `SELECT ... LIMIT 1` on a
 * partial index costs approximately nothing. This is NOT the pace of requests
 * to Douyin -- that is `sleepMsMin/Max`, and it lives between PAGES.
 */
export const ENUMERATE_IDLE_MS = 1_000;

/**
 * Consecutive HTTP 400s before the job is parked at `blocked`.
 *
 * IDEA.md §6.2's number, kept, but NOT its conclusion -- see `runOnce`'s note
 * on why phase 5 refuses to name a cause.
 */
export const MAX_CONSECUTIVE_REJECTIONS = 3;

/**
 * Just enough of `ParserClient` for this lane, for the same reason
 * `LaneRunner` takes a `LaneLogger` rather than a Nest logger: the lane can
 * then be constructed and ticked against a scripted page source in a plain
 * unit test, with no container and no HTTP server. `ParserClient` satisfies
 * this structurally; nothing needs to implement it explicitly.
 */
export interface PostPageSource {
  fetchUserPostPage(secUserId: string, maxCursor: number, count: number): Promise<UserPostPage>;
}

export interface EnumerateLaneOptions {
  /** `LEASE_SECONDS`. Renewed on every page (see `checkpoint`). */
  leaseSeconds: number;
  /** `URL_TTL_SECONDS`. How long a cached `play_addr` is assumed good (F1). */
  urlTtlSeconds: number;
  /** `ENUMERATE_SLEEP_MS_MIN` -- floor of the between-pages delay. */
  sleepMsMin: number;
  /** `ENUMERATE_SLEEP_MS_MAX` -- ceiling of the same. */
  sleepMsMax: number;
  /** Test seam for the jitter. Never set in production. */
  random?: () => number;
}

/** How one job's enumeration ended. Each ending owes the claim something different. */
type EnumerateOutcome =
  /** Reached the last page. The job moves on to `downloading`. */
  | 'finished'
  /** The cookie is being refused. Park at `blocked`, keep the checkpoint. */
  | 'blocked'
  /** Reaped, cancelled, or finished by somebody else. Touch nothing. */
  | 'lost'
  /** Shutdown. Keep holding the claim so `stop()` hands it back. */
  | 'interrupted';

/**
 * The enumerate lane: pages a profile, one page at a time, one job at a time.
 *
 * F2 -- ENUMERATE CONCURRENCY IS 1 FOR THE WHOLE SYSTEM, NOT 1 PER JOB.
 *
 * Douyin rate-limits by IP, not by job. Three operators starting three jobs is
 * three times the request rate from one address, which is a ban -- so the
 * second job WAITS. That guarantee is not enforced anywhere in this file: it is
 * the number of instances registered in `lanes.module.ts`, which is Approach
 * 1's whole point (solution design §4). There is no semaphore to miscount and
 * no config value to get wrong; there is one entry in an array, and a second
 * one would be a diff. `test/lanes/enumerate-global-concurrency.spec.ts`
 * measures the request rate through the production wiring so that the diff
 * cannot pass.
 *
 * If you are about to make this enumerate several jobs at once -- or to add a
 * second instance to that array "for throughput" -- the throughput ceiling is
 * not this lane, it is the IP.
 *
 * The other reason A1 was chosen is visible in `enumerate`: the `sleep` sits
 * INSIDE the paging loop, between one request and the next. A scheduler
 * interval one layer up can pace ticks; it cannot pace pages within a tick,
 * and pages within a tick is where the request budget is actually spent.
 */
export class EnumerateLane extends LaneRunner {
  readonly name = 'enumerate';
  readonly idleDelayMs = ENUMERATE_IDLE_MS;

  /** Test seam: reports each between-pages delay actually drawn. */
  onDelay?: (ms: number) => void;

  private readonly random: () => number;

  constructor(
    logger: LaneLogger,
    private readonly db: Executor,
    private readonly parser: PostPageSource,
    private readonly opts: EnumerateLaneOptions,
  ) {
    super(logger);
    this.random = opts.random ?? Math.random;
  }

  /**
   * Claim one job, page it to the end, let go of it.
   *
   * The claim is registered with `hold()` so that a `SIGTERM` arriving
   * mid-enumerate hands the job straight back to `pending` (§4.2) instead of
   * leaving it for the reaper to recover a full `LEASE_SECONDS` later.
   *
   * Every ending except `interrupted` clears the hold, because in each of those
   * the job has already been moved out of `enumerating` -- or was taken from us
   * -- and releasing it afterwards would drag a `downloading` job back onto the
   * enumerate queue, re-paging its whole profile.
   *
   * A THROWN tick releases the claim and then rethrows. Both halves are
   * deliberate: releasing means the next tick can retry immediately, and
   * rethrowing is what makes the base class log the failure and back off, so a
   * parser that is down does not become a hot loop against it.
   */
  async runOnce(signal: AbortSignal): Promise<TickOutcome> {
    const job = await jobsRepository.claimOne(this.db, { leaseSeconds: this.opts.leaseSeconds });
    if (!job) return 'idle';

    // `job.id` only -- never the profile URL or anything derived from a cookie.
    const release = (): Promise<void> =>
      jobsRepository.releaseClaim(this.db, job.id).then(() => undefined);
    this.hold({ describe: `enumerate claim on job ${job.id}`, release });

    let outcome: EnumerateOutcome;
    try {
      outcome = await this.enumerate(job, signal);
    } catch (err: unknown) {
      this.clearHold();
      // The release is best-effort and must not become the reported failure.
      // Without the catch, a release that throws (Postgres gone -- the most
      // likely reason it would) REPLACES the parser error that actually caused
      // the tick to fail, so the log names the second-order symptom and the
      // first-order cause is lost. The claim then falls to the reaper either
      // way, which is the outcome this release exists to improve on, not to
      // guarantee.
      await release().catch((releaseErr: unknown) => {
        this.logger.error(
          `lane ${this.name}: failed to hand job ${job.id} back after a failed tick; ` +
            `the reaper will recover it when the lease expires: ${describe(releaseErr)}`,
        );
      });
      throw err;
    }

    if (outcome !== 'interrupted') this.clearHold();
    return 'worked';
  }

  /**
   * The paging loop of §5.2.
   *
   * Reading order, because every line of it is a decision:
   *
   *   - the cursor starts at the CHECKPOINT, not at zero. That is what makes a
   *     killed run resume rather than re-page (F5).
   *   - the blocked check comes BEFORE the insert, because a blocked page has
   *     nothing to insert and the next thing to do is stop.
   *   - the checkpoint is written after EVERY page and before the sleep. Moving
   *     it below the loop turns a crash at page fourteen into a re-scan from
   *     page one.
   *   - the sleep is the LAST thing in the body, so it never runs after the
   *     final page: a delay with no request after it is pure latency.
   */
  private async enumerate(job: Job, signal: AbortSignal): Promise<EnumerateOutcome> {
    let cursor = job.maxCursor;
    let enumerated = job.enumeratedCount;
    let rejections = 0;
    let pages = 0;
    let inserted = 0;
    let dropped = 0;

    for (;;) {
      if (signal.aborted) {
        this.logger.log(
          `lane ${this.name}: shutting down mid-enumerate of job ${job.id} after ` +
            `${pages} page(s); handing the job back at cursor ${cursor}`,
        );
        return 'interrupted';
      }

      let page: UserPostPage;
      try {
        page = await this.parser.fetchUserPostPage(job.secUserId, cursor, ENUMERATE_PAGE_SIZE);
        rejections = 0;
      } catch (err: unknown) {
        // 400 ONLY. The fork answers 400 for every internal exception and its
        // body carries no diagnostic information whatsoever (F4), so a 400 is
        // the one refusal whose cause is unknown by construction and therefore
        // worth retrying. Any other status -- 404, 422 -- means we and the fork
        // disagree about the route or its parameters, which is a bug on this
        // side that retrying cannot fix and that must not be reported as a
        // cookie problem.
        if (!(err instanceof ParserRejectedError) || err.status !== 400) throw err;

        rejections += 1;
        if (rejections < MAX_CONSECUTIVE_REJECTIONS) {
          this.logger.warn(
            `lane ${this.name}: job ${job.id} page at cursor ${cursor} refused with HTTP 400 ` +
              `(${rejections}/${MAX_CONSECUTIVE_REJECTIONS} consecutive); retrying the same page`,
          );
          await this.pause(signal);
          continue;
        }

        // TODO(phase-7): F4's probe goes here. Phase 5 deliberately does NOT
        // guess. Three 400s could be a banned cookie, a private profile, a
        // deleted account, or a crawler bug, and the response is different for
        // each -- so this parks the job with the cause stated as UNKNOWN rather
        // than sending an operator to rotate a cookie that may be fine. Phase 7
        // adds the one observation that can tell them apart: a probe of a
        // known-public profile with the same cookie.
        await this.park(
          job,
          `stopped after ${MAX_CONSECUTIVE_REJECTIONS} consecutive HTTP 400s from the parser. ` +
            `The cause is not diagnosed: the parser returns 400 for every internal failure with ` +
            `no detail, so this could be a refused cookie, a private profile, or a deleted ` +
            `account. The checkpoint is kept, so /retry resumes from here.`,
        );
        return 'blocked';
      }

      // §5.3 -- the STRUCTURAL signal, and the only diagnosis this phase makes.
      // An empty list paired with `has_more` set is a certain verdict and
      // arrives as an HTTP 200, which is why it needs no probe. Note the
      // pairing IS the signal: an empty page with `has_more` UNSET is simply
      // the end of a profile whose item count divides exactly by the page size,
      // and calling that a banned cookie would be the F4 mistake in reverse.
      if (page.awemeList.length === 0 && page.hasMore) {
        await this.park(
          job,
          'the profile page came back empty while still reporting more pages, which means the ' +
            'cookie is being refused. The checkpoint is kept, so pushing a fresh cookie and ' +
            'calling /retry resumes from here.',
        );
        return 'blocked';
      }

      // Douyin's cursor is the create_time of the OLDEST item in the page and a
      // profile pages newest-first, so a cursor that claims more pages must have
      // MOVED BACK IN TIME. Anything else is a loop: equal means the next
      // request is byte-for-byte the one just made, and larger walks back
      // towards the newest video so page one comes round again. Either way the
      // lane spends the IP-based rate limit at full speed forever, `runOnce`
      // never returns so the queue behind it never advances, and every page of
      // it is a healthy HTTP 200.
      //
      // `cursor === 0` is the one exception and it is not an edge case: it is
      // how every job starts, and a first page legitimately answers with a large
      // millisecond timestamp. Checked before the write, so a cursor that cannot
      // be paged from never reaches the checkpoint.
      //
      // Rejecting the "went up" half is only possible because the direction is
      // KNOWN -- the same fact `checkpoint`'s rule rests on, used twice.
      const nextCursor = Math.trunc(page.maxCursor);
      if (page.hasMore && cursor !== 0 && nextCursor >= cursor) {
        throw new Error(
          `lane ${this.name}: job ${job.id} was handed cursor ${nextCursor} while paging from ` +
            `${cursor} with has_more set. Douyin's cursor only ever decreases, so following ` +
            `this one would re-request pages it has already read, forever.`,
        );
      }

      const rows = page.awemeList.map((item) => toVideoUpsert(item, job.secUserId));
      const usable = rows.filter((row): row is NonNullable<typeof row> => row !== null);
      dropped += rows.length - usable.length;
      inserted += await videosRepository.upsertMany(this.db, usable, {
        urlTtlSeconds: this.opts.urlTtlSeconds,
      });

      // Counts what Douyin SENT, not what was inserted. Both later uses need
      // that: it is the progress figure an operator watches, and it is the
      // yardstick `checkpoint` gates the cursor on -- and a page of pure
      // duplicates (the normal case on a re-run) inserts nothing while still
      // being real progress through the profile.
      enumerated += page.awemeList.length;
      // Truncated, because `max_cursor` is a bigint column and the parser's
      // number coercion accepts non-integers: a cursor of `...600000.5` reaches
      // the parameter and Postgres rejects the whole statement with `invalid
      // input syntax for type bigint`. The tick would fail safely -- claim
      // released, base class backs off -- and then do it again on every retry,
      // forever, with nothing in the log mentioning a decimal point.
      cursor = nextCursor;
      pages += 1;

      // A page that added nothing cannot checkpoint: `checkpoint` requires
      // strictly more items than the row already holds, so a no-op page would be
      // reported as "not ours" and abandon a job that is merely finished. It
      // cannot be the blocked signal either -- that is handled above -- so an
      // empty page here is simply the end of a profile whose item count divides
      // exactly by the page size, and the loop is done.
      if (page.awemeList.length === 0) break;

      const stillOurs = await jobsRepository.checkpoint(this.db, job.id, {
        maxCursor: cursor,
        enumeratedCount: enumerated,
        leaseSeconds: this.opts.leaseSeconds,
      });
      if (!stillOurs) {
        // Reaped, cancelled, or completed by somebody else while we were
        // paging. It may already belong to another lane, so the one thing not
        // to do is hand it back underneath them.
        this.logger.warn(
          `lane ${this.name}: job ${job.id} is no longer ours after ${pages} page(s); stopping`,
        );
        return 'lost';
      }

      if (!page.hasMore) break;

      // LAST in the body, so no delay follows the final page. INSIDE the loop,
      // so it paces requests rather than ticks -- the thing a scheduler
      // interval one layer up cannot express, and reason #1 for A1.
      await this.pause(signal);
    }

    const finished = await jobsRepository.finishEnumerate(this.db, job.id);
    if (!finished) return 'lost';

    this.logger.log(
      `lane ${this.name}: job ${job.id} enumerated ${pages} page(s), ${enumerated} item(s), ` +
        `${inserted} new video(s)` +
        (dropped > 0 ? `, ${dropped} item(s) dropped as unusable` : '') +
        `; job is now downloading`,
    );
    return 'finished';
  }

  /**
   * Parks the job at `blocked` -- never `failed` -- and says so.
   *
   * The distinction is the whole remediation signal: both statuses are
   * retryable, so the WORD is what tells an operator to rotate the cookie
   * rather than write the profile off. `markBlocked` keeps `max_cursor`, which
   * is what makes the recovery cheap.
   */
  private async park(job: Job, reason: string): Promise<void> {
    await jobsRepository.markBlocked(this.db, job.id, reason);
    this.logger.warn(`lane ${this.name}: job ${job.id} is BLOCKED (not failed): ${reason}`);
  }

  /**
   * The between-pages delay: `sleepMsMin..sleepMsMax`, jittered.
   *
   * Jittered rather than fixed because a metronome is a fingerprint, and
   * bounded on both sides because the request budget has to stay predictable.
   * Passing `signal` is what lets a shutdown cut a wait short instead of
   * holding `docker stop` open until it SIGKILLs -- see sleep.ts.
   */
  private async pause(signal: AbortSignal): Promise<void> {
    const span = Math.max(0, this.opts.sleepMsMax - this.opts.sleepMsMin);
    const ms = this.opts.sleepMsMin + Math.floor(this.random() * (span + 1));
    this.onDelay?.(ms);
    await sleep(ms, signal);
  }
}

/** Renders anything throwable as a log line -- see `lane.runner.ts`'s note on
 * why this is not `err.message` and not an `instanceof` check. */
function describe(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}
