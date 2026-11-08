import type { Executor } from '../db/executor';
import * as jobsRepository from '../db/jobs.repository';
import * as videosRepository from '../db/videos.repository';
import type { Job } from '../db/schema';
import { ParserRejectedError } from '../parser/parser.errors';
import { probeCookie, type ProbeSource } from '../parser/f4-probe';
import type { UserPostPage } from '../parser/parser.client';
import { toVideoUpsert } from '../parser/aweme.mapper';
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

/**
 * Everything this lane asks of a parser: pages, and -- since phase 7 -- the F4
 * probe.
 *
 * Composed rather than folded into `PostPageSource` so that each half still says
 * what it is, and REQUIRED rather than optional. An optional probe would mean a
 * lane wired without one silently degrades to phase 5's "cause UNKNOWN" park,
 * and the mutation that unwires it in `lanes.module.ts` would leave every test
 * in this file green -- a guard nobody is watching, which is the failure mode
 * plan §5 is a list of.
 */
export type EnumerateParser = PostPageSource & ProbeSource;

export interface EnumerateLaneOptions {
  /** `LEASE_SECONDS`. Renewed on every page (see `checkpoint`). */
  leaseSeconds: number;
  /** `URL_TTL_SECONDS`. How long a cached `play_addr` is assumed good (F1). */
  urlTtlSeconds: number;
  /** `ENUMERATE_SLEEP_MS_MIN` -- floor of the between-pages delay. */
  sleepMsMin: number;
  /** `ENUMERATE_SLEEP_MS_MAX` -- ceiling of the same. */
  sleepMsMax: number;
  /**
   * `PROBE_SEC_USER_ID` -- the known-public profile the F4 probe asks about.
   *
   * Not optional, and not defaulted to anything: a probe pointed at a profile
   * that is itself private or deleted can never answer `cookie-healthy`, so
   * every 3x400 would be reported as `blocked` and F4 would be silently
   * inverted. See `ProbeHealthCheck` for the boot-time check that says so out
   * loud, and phase-07's risk register for why that check is loud rather than
   * fatal.
   */
  probeSecUserId: string;
  /** Test seam for the jitter. Never set in production. */
  random?: () => number;
}

/** How one job's enumeration ended. Each ending owes the claim something different. */
type EnumerateOutcome =
  /** Reached the last page. The job moves on to `downloading`. */
  | 'finished'
  /** The cookie is being refused. Park at `blocked`, keep the checkpoint. */
  | 'blocked'
  /**
   * F4: the probe proved the cookie good, so this PROFILE is the problem.
   * `failed`, keeping the checkpoint -- see `jobsRepository.markFailed`.
   */
  | 'failed'
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
    private readonly parser: EnumerateParser,
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
    /**
     * 图文 image posts seen in this run, recorded as `skipped` rather than
     * queued (`aweme.mapper.ts`). Counts what the PAGES CARRIED, like
     * `enumerated` and unlike `inserted` -- a re-run counts them again, because
     * the number answers "how much of this profile is not video" rather than
     * "what did this run write".
     */
    let imagePosts = 0;

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

        // F4 -- THE PROBE. Phase 5 parked here with the cause stated as
        // UNKNOWN, because a 400 from the fork carries no diagnostic
        // information at all (`parser.errors.ts`) and guessing would send an
        // operator to rotate a cookie that may be fine. Phase 7 adds the one
        // observation the failing call cannot supply: ask the SAME parser, with
        // the SAME cookie, about a profile that is KNOWN to be public. Whatever
        // happens to that request is about the cookie, the signing, or the
        // network -- it cannot be about the profile that just failed.
        //
        // This costs exactly ONE extra request and only ever on this path,
        // which is why §7.1 puts it third: the structural signal below is an
        // HTTP 200 and certain, and the retries above are cheaper. See
        // `f4-probe.ts` for the five outcomes and what each one means.
        return this.diagnose(job, cursor);
      }

      // §5.3 -- the STRUCTURAL signal, and the only diagnosis this phase makes.
      // An empty list paired with `has_more` set is a certain verdict and
      // arrives as an HTTP 200, which is why it needs no probe. Note the
      // pairing IS the signal: an empty page with `has_more` UNSET is simply
      // the end of a profile whose item count divides exactly by the page size,
      // and calling that a banned cookie would be the F4 mistake in reverse.
      if (page.awemeList.length === 0 && page.hasMore) {
        const parked = await this.park(
          job,
          'the profile page came back empty while still reporting more pages, which means the ' +
            'cookie is being refused. The checkpoint is kept, so pushing a fresh cookie and ' +
            'calling /retry resumes from here.',
        );
        return parked ? 'blocked' : this.lost(job);
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
      imagePosts += usable.filter((row) => row.status === 'skipped').length;
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
        // Said out loud, because "215 items, 213 videos" is the difference an
        // operator otherwise has to work out by subtraction -- and not working
        // it out is how two MP3s spent a whole run reported as `done`.
        (imagePosts > 0
          ? `, ${imagePosts} image post(s) skipped (no video to download)`
          : '') +
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
  private async park(job: Job, reason: string): Promise<boolean> {
    const parked = await jobsRepository.markBlocked(this.db, job.id, reason);
    if (parked) {
      this.logger.warn(`lane ${this.name}: job ${job.id} is BLOCKED (not failed): ${reason}`);
    }
    return parked;
  }

  /**
   * The claim went away before we could record a verdict.
   *
   * Reaped, cancelled, or finished by somebody else -- so the row belongs to
   * someone now and the one thing not to do is write over it. Said out loud
   * because the alternative is a diagnosis that was reached, cost a probe, and
   * then vanished with nothing anywhere explaining why the job carried on.
   */
  private lost(job: Job): EnumerateOutcome {
    this.logger.warn(
      `lane ${this.name}: reached a verdict for job ${job.id} but the claim was already ` +
        `gone (reaped, cancelled, or finished elsewhere), so nothing was written`,
    );
    return 'lost';
  }

  /**
   * F4 -- runs the probe and writes whichever verdict it supports.
   *
   * The ONLY place in this service that turns "three 400s" into a diagnosis, and
   * the branch is one line because the whole judgement lives in
   * `probeCookie`: `global` means the fault is the cookie / the signing / the
   * network, so the job is `blocked` and an operator has something to fix;
   * `!global` means the probe RULED THOSE OUT, so the job is `failed` and
   * nobody touches a working cookie.
   *
   * Both endings keep `max_cursor`, so either way /retry resumes from here
   * rather than re-spending the F2 request budget.
   *
   * A probe that throws something other than a `ParserError` is not caught: that
   * is a bug on this side, and burying it in a story about Douyin is precisely
   * what F4 exists to stop.
   */
  private async diagnose(job: Job, cursor: number): Promise<EnumerateOutcome> {
    const probe = await probeCookie(this.parser, this.opts.probeSecUserId);
    const preamble =
      `stopped after ${MAX_CONSECUTIVE_REJECTIONS} consecutive HTTP 400s from the parser at ` +
      `cursor ${cursor}. The 400 itself carries no detail, so a probe of a known-public profile ` +
      `was run with the same cookie: `;
    const trailer = ' The checkpoint is kept, so /retry resumes from here.';

    // Each writer reports whether it actually landed, and `lost` is the honest
    // answer when it did not. THE PROBE IS WHY THIS MATTERS NOW: it is a network
    // call with a 30s budget and it does not renew the lease, so the reaper has a
    // real window in which to hand this job to somebody else while we wait. Both
    // writers are gated on `status = 'enumerating'`, so the write then quietly
    // affects nothing -- and the phase-5 version of this code logged "job X is
    // BLOCKED" regardless, which is a log line asserting the opposite of what
    // happened about a job that is `pending` under a new claim.
    if (!probe.global) {
      if (!(await this.giveUp(job, preamble + probe.reason + trailer))) return this.lost(job);
      return 'failed';
    }
    if (!(await this.park(job, preamble + probe.reason + trailer))) return this.lost(job);
    return 'blocked';
  }

  /**
   * Writes `failed` -- the verdict that says a new cookie will change nothing.
   *
   * Separate from `park` rather than a parameter of it, because the two are
   * different instructions to a human and the log line has to say which one was
   * chosen and WHY. A single function with a status argument would make the two
   * call sites look interchangeable, and they are the exact pair F4 exists to
   * keep apart.
   */
  private async giveUp(job: Job, reason: string): Promise<boolean> {
    const failed = await jobsRepository.markFailed(this.db, job.id, reason);
    if (failed) {
      this.logger.warn(
        `lane ${this.name}: job ${job.id} FAILED (not blocked -- the probe proved the cookie is ` +
          `still good, so rotating it would not help): ${reason}`,
      );
    }
    return failed;
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
