import { Transform } from 'node:stream';
import type { CdnClient } from '../cdn/cdn.client';
import type { Executor } from '../db/executor';
import * as jobsRepository from '../db/jobs.repository';
import * as videosRepository from '../db/videos.repository';
import type { Video } from '../db/schema';
import type { VideoPlayAddr } from '../parser/parser.client';
import type { Storage } from '../storage/storage.interface';
import { videoKey } from '../storage/storage.interface';
import { LaneRunner, type LaneLogger, type TickOutcome } from './lane.runner';

/**
 * How long to wait before asking for a video again when there was none.
 *
 * Same number and same reasoning as the enumerate lane's: the queue is
 * Postgres and the claim is a partial-index lookup, so a one-second poll costs
 * approximately nothing. It is emphatically NOT a request rate towards Douyin
 * -- the CDN is not risk-controlled (§6.1), which is exactly why download
 * concurrency is 6 while enumerate's is 1.
 */
export const DOWNLOAD_IDLE_MS = 1_000;

/**
 * CDN statuses that mean "that signature is stale, ask for another one".
 *
 * F1's reactive half. These are the ONLY two treated as an instruction rather
 * than a failure: a 403 or 410 from a signed URL says the URL died, which
 * re-resolving fixes. Anything else (a 500, a 404 for a deleted video) is not
 * fixed by a new URL and must not spend a request on the rate-limited API.
 */
const REFUSALS = new Set([403, 410]);

/**
 * Just enough of `ParserClient` for the re-resolve path, for the same reason
 * `EnumerateLane` takes a `PostPageSource`: the lane can then be ticked
 * against a scripted resolver in a plain unit test -- which is what makes
 * F1's "zero calls" DoD line measurable at all. `ParserClient` satisfies this
 * structurally.
 */
export interface VideoUrlSource {
  fetchOneVideo(awemeId: string): Promise<VideoPlayAddr>;
}

export interface DownloadLaneOptions {
  /** 1-based position in the `LANES` array; only used to name the lane. */
  index: number;
  /** `LEASE_SECONDS`. Renewed from the stream's own progress -- see `heartbeat`. */
  leaseSeconds: number;
  /** `MAX_ATTEMPTS`. Already spent at claim time (F6). */
  maxAttempts: number;
  /** `URL_TTL_SECONDS`. How long a freshly resolved `play_addr` is cached. */
  urlTtlSeconds: number;
  /**
   * How often, while bytes are moving, to renew the lease and re-ask whether
   * the job is still wanted. Defaults to a third of the lease, so two
   * consecutive missed beats still leave time to notice.
   */
  heartbeatMs?: number;
}

/** How one claim ended. Each ending owes the row something different. */
type DownloadOutcome =
  /** Committed and marked `done`. */
  | 'done'
  /** The job was cancelled. The row is `skipped`; nothing went wrong. */
  | 'skipped'
  /** The claim was taken from us mid-download. Touch NOTHING. */
  | 'lost'
  /** Shutdown. Keep holding the claim so `stop()` hands it back. */
  | 'interrupted';

/**
 * The download lane: claim one video, fetch it, commit it, repeat.
 *
 * DOWNLOAD CONCURRENCY IS THE NUMBER OF THESE, NOT A SEMAPHORE (§6.1).
 * `lanes.module.ts` registers `DOWNLOAD_CONCURRENCY` instances and each runs
 * its own loop, so "at most N concurrent downloads" is true by construction
 * rather than by a counter that can be miscounted. Six is safe where
 * enumerate's one is not, and the difference is not a guess: `*.douyinvod.com`
 * serves bytes and is not behind the risk control that rate-limits the web API
 * by IP (IDEA.md §3.1 vs §3.2).
 *
 * The two invariants this class exists to hold, both with mutation tests in
 * `test/mutations/phase-06.sh`:
 *
 *   1. THE OWNERSHIP RE-CHECK BEFORE THE COMMIT. The reaper hands a video whose
 *      lease expired to another worker (F3), and an expired lease does not mean
 *      the old worker is dead -- a slow CDN gets reaped mid-stream. Both workers
 *      then hold the same key. The temp name is unique per put so their bytes
 *      cannot mix, and this lane re-checks its claim inside `Storage.put`,
 *      immediately before the rename, so the straggler declines to commit at
 *      all. Without it the last rename wins and that may be the stale one.
 *   2. THE ADVERTISED LENGTH IS VERIFIED. A body that stops early is otherwise
 *      indistinguishable from a complete one, and the video would be `done`
 *      with a short file nobody discovers until playback.
 */
export class DownloadLane extends LaneRunner {
  readonly name: string;
  readonly idleDelayMs = DOWNLOAD_IDLE_MS;

  private readonly heartbeatMs: number;

  constructor(
    logger: LaneLogger,
    private readonly db: Executor,
    private readonly parser: VideoUrlSource,
    private readonly cdn: CdnClient,
    private readonly storage: Storage,
    private readonly opts: DownloadLaneOptions,
  ) {
    super(logger);
    // Numbered, not just `download`. Six identically-named loops make the
    // staleness alert (§4.1) say "a lane is stuck" without saying which, and a
    // wedged socket in one of six is precisely the case it is for.
    this.name = `download-${opts.index}`;
    this.heartbeatMs = opts.heartbeatMs ?? Math.max(1_000, (opts.leaseSeconds * 1_000) / 3);
  }

  /**
   * One video, start to finish.
   *
   * The claim is registered with `hold()` so a `SIGTERM` mid-download hands the
   * video straight back to `pending` (§4.2) instead of leaving it for the
   * reaper a full `LEASE_SECONDS` later.
   *
   * A THROWN tick records the failure on the ROW and then rethrows. Both halves
   * matter: `markFailed` is what spends the attempt honestly and what an
   * operator reads, and the rethrow is what makes the base class log it and
   * back off -- so a CDN that is refusing everything does not become a hot loop
   * against it. §6.3's "exponential backoff" is that backoff; the "3 lần" is
   * `attempts`, already incremented at claim time (F6), which is why the bound
   * holds even for a worker that is killed and never reports anything.
   */
  async runOnce(signal: AbortSignal): Promise<TickOutcome> {
    // Checked before claiming: taking a video we are about to hand back spends
    // an attempt (F6) for nothing.
    if (signal.aborted) return 'idle';

    const claimed = await videosRepository.claimOne(this.db, {
      leaseSeconds: this.opts.leaseSeconds,
      maxAttempts: this.opts.maxAttempts,
    });
    if (!claimed) return 'idle';

    // `attempts` at claim time IS the claim token -- see `ownsClaim` in
    // videos.repository.ts. Captured once, here, because every write this tick
    // makes to the row is gated on it.
    const token = claimed.attempts;
    const give = { maxAttempts: this.opts.maxAttempts, attempts: token };
    // `awemeId` only. `play_addr` is a signed URL whose query is a credential.
    this.hold({
      describe: `download claim on video ${claimed.awemeId}`,
      // The boolean is not decoration. `release` is gated on still owning the
      // claim, and the case where it does NOT land is real: a lease that lapsed
      // mid-download -- which the heartbeat's progress gate deliberately allows
      // for a wedged socket -- gives back nothing. `LaneRunner.releaseHeld`
      // logs "released ... on shutdown" either way, so without this line the
      // log asserts the opposite of what happened and phase 4's "restart picks
      // it up IMMEDIATELY" is quietly false for exactly that video.
      release: async () => {
        if (await videosRepository.release(this.db, claimed.awemeId, give)) return;
        this.logger.warn(
          `lane ${this.name}: could not hand video ${claimed.awemeId} back on shutdown -- ` +
            `this claim is no longer live, so the reaper will recover the row instead`,
        );
      },
    });

    let outcome: DownloadOutcome;
    try {
      outcome = await this.download(claimed, signal);
    } catch (err: unknown) {
      this.clearHold();
      // Best effort, and it must not become the reported failure: without the
      // catch, a `markFailed` that throws (Postgres gone, the most likely
      // reason) REPLACES the CDN error that actually caused the tick to fail,
      // so the log names the second-order symptom. Same shape as the enumerate
      // lane's release path, same reason.
      const recorded = await videosRepository
        .markFailed(this.db, claimed.awemeId, describe(err), give)
        .catch((writeErr: unknown) => {
          this.logger.error(
            `lane ${this.name}: failed to record the failure of video ${claimed.awemeId}; ` +
              `the reaper will recover it when the lease expires: ${describe(writeErr)}`,
          );
          return false;
        });
      // A give-up is a settling too. `markFailed` writes `failed` once the
      // attempts are spent, and if that was the profile's last outstanding
      // video then the job is finished -- with failures, but finished. Skipping
      // the check here would leave such a job at `downloading` until the
      // reaper's 60s sweep noticed, which reads to an operator as a stuck job
      // (§6.3: "đi tiếp, không kill cả job" -- and not "leave it hanging"
      // either).
      // Guarded for the same reason `markFailed` is: this is the failure path,
      // and a throw from here would REPLACE the error that actually caused the
      // tick to fail. `finalizeSettled` is also exactly what the reaper does on
      // its 60s sweep, so losing this one is a delay rather than a lost job --
      // whereas losing the cause is a log line naming the wrong problem.
      if (!recorded) {
        // The failure was NOT written: this claim is no longer live, so the row
        // belongs to somebody else (or to the reaper) and must not be touched.
        // Said out loud because the alternative is an attempt that failed with
        // no `last_error` and no trace of why anywhere.
        this.logger.warn(
          `lane ${this.name}: could not record the failure of video ${claimed.awemeId} -- ` +
            `this claim is no longer live; the reaper owns the row now`,
        );
      }
      if (recorded) {
        await this.settle(claimed.secUserId).catch((settleErr: unknown) => {
          this.logger.error(
            `lane ${this.name}: failed to check whether profile work is finished after ` +
              `video ${claimed.awemeId} gave up; the reaper's finalize sweep will catch it: ` +
              `${describe(settleErr)}`,
          );
        });
      }
      throw err;
    }

    if (outcome === 'interrupted') return 'worked';
    this.clearHold();
    // `lost` is the one settled-looking outcome that must NOT finalize: the row
    // is still `downloading` under somebody else's claim, so the profile does
    // still owe work and asking would only take a needless lock.
    if (outcome !== 'lost') await this.settle(claimed.secUserId);
    return 'worked';
  }

  /**
   * §6.2, in order. Every step is a decision:
   *
   *   - the cancelled check comes FIRST, before any request: a cancelled job's
   *     video costs zero bytes.
   *   - the URL is taken from the row when it is fresh (F1) and re-resolved
   *     only when it is not, or when the CDN refuses it.
   *   - the commit is `Storage.put`'s rename, gated on still owning the claim.
   *   - the job is finalized only after the row is `done`, so a crash between
   *     the two leaves work to do rather than a job claiming to be finished.
   */
  private async download(video: Video, signal: AbortSignal): Promise<DownloadOutcome> {
    const token = video.attempts;

    if (await jobsRepository.isProfileCancelled(this.db, video.secUserId)) {
      // Cancelled, not failed. Nothing went wrong, so nothing should read as
      // wrong -- and `markSkipped` deliberately leaves `last_error` null.
      if (!(await videosRepository.markSkipped(this.db, video.awemeId, token))) return 'lost';
      this.logger.log(
        `lane ${this.name}: video ${video.awemeId} skipped, its job was cancelled`,
      );
      return 'skipped';
    }

    // F1. `video.playAddr` comes off the row this tick claimed, and the claim
    // is exclusive, so it is the authoritative value -- but WHETHER it is still
    // fresh is a question about `now()`, and that has to be asked of Postgres.
    // The expiry was written from the database clock (`upsertMany`), so reading
    // it against `Date.now()` would let a host a minute out of step either
    // re-resolve URLs that are fine -- spending the budget F1 exists to protect
    // -- or trust ones that are not.
    const cached =
      video.playAddr !== null && (await videosRepository.isUrlFresh(this.db, video.awemeId))
        ? video.playAddr
        : null;

    let url = cached ?? (await this.resolve(video.awemeId, 'the cached URL was missing or expired'));
    // At most ONE re-resolve per attempt, whatever triggered it. A CDN
    // answering 403 to everything would otherwise become an unbounded
    // `fetch_one_video` loop against the rate-limited API -- F1's own failure
    // mode, reached from the opposite direction. The next attempt gets a fresh
    // budget, and `attempts` bounds how many attempts there are.
    let resolved = cached === null;

    for (;;) {
      const status = await this.transfer(video, url, signal);
      if (status === 'committed') return 'done';
      if (status === 'lost') return 'lost';
      if (status === 'cancelled') {
        if (!(await videosRepository.markSkipped(this.db, video.awemeId, token))) return 'lost';
        this.logger.log(
          `lane ${this.name}: video ${video.awemeId} was cancelled mid-download; ` +
            `nothing was committed`,
        );
        return 'skipped';
      }
      if (status === 'interrupted') return 'interrupted';

      // A refusal. One re-resolve, then the attempt is over.
      if (resolved) {
        throw new Error(
          `cdn refused video ${video.awemeId} with HTTP ${status.refused} using a play_addr ` +
            `this attempt had already resolved once; not spending another API request on it`,
        );
      }
      url = await this.resolve(video.awemeId, `the cdn answered HTTP ${status.refused}`);
      resolved = true;
    }
  }

  /**
   * One GET and, if it is served, one commit attempt.
   *
   * Returns what the caller has to decide about: committed, refused (so
   * re-resolve), lost (the claim went to somebody else), cancelled, or
   * interrupted by shutdown. Anything genuinely wrong throws.
   */
  private async transfer(
    video: Video,
    url: string,
    signal: AbortSignal,
  ): Promise<'committed' | 'lost' | 'cancelled' | 'interrupted' | { refused: number }> {
    const token = video.attempts;
    // A controller of OUR OWN, chained to the lane's. The heartbeat has to be
    // able to abort this one download -- because the lease was lost, or the job
    // was cancelled -- without aborting the lane, and a shutdown has to abort
    // both. Chaining rather than reusing the lane's signal is what keeps those
    // two separable.
    const aborter = new AbortController();
    const onLaneAbort = (): void => aborter.abort();
    signal.addEventListener('abort', onLaneAbort, { once: true });
    if (signal.aborted) aborter.abort();

    /** Set by the heartbeat, so the caller can tell WHY the stream stopped. */
    let lost = false;
    let cancelled = false;
    /** Set by the commit hook: the CDN answered 200 and sent nothing. */
    let empty = false;

    try {
      const res = await this.cdn.get(url, aborter.signal);

      if (REFUSALS.has(res.status)) {
        // The body is read and thrown away rather than left: undici keeps a
        // connection pinned until its body is consumed, and a profile is
        // hundreds of videos.
        await res.discard();
        return { refused: res.status };
      }
      if (res.status !== 200) {
        await res.discard();
        throw new Error(
          `cdn answered HTTP ${res.status} for video ${video.awemeId}, which no amount of ` +
            `re-resolving fixes`,
        );
      }

      if (res.contentLength === undefined) {
        // Said out loud, because it is the one case where the truncation guard
        // below cannot help: with nothing advertised there is nothing to compare
        // the byte count against, so a body that stops early is committed. Not
        // an error -- a chunked response is legal and Douyin's CDN does send
        // them -- but an operator chasing a corrupt file deserves to find this
        // line rather than infer it.
        this.logger.warn(
          `lane ${this.name}: cdn advertised no Content-Length for video ${video.awemeId}; ` +
            `the transfer cannot be length-verified`,
        );
      }

      // Bytes counted on the way past, so the heartbeat can renew the lease on
      // the strength of actual PROGRESS and not merely of time passing. A
      // wedged socket must not be able to hold a claim open indefinitely by
      // renewing a lease it is not earning -- the same rule the enumerate
      // checkpoint follows.
      let transferred = 0;
      const metered = new Transform({
        transform(chunk: Buffer, _encoding, callback) {
          transferred += chunk.length;
          callback(null, chunk);
        },
      });
      // `.pipe` does not forward errors, and the error is the interesting case:
      // a CDN connection dropped mid-body arrives here and must reach the
      // storage pipeline as a failure, not as a clean end-of-stream.
      res.body.on('error', (err) => metered.destroy(err));
      res.body.pipe(metered);

      const beat = this.startHeartbeat(video, () => transferred, {
        onLost: () => {
          lost = true;
          aborter.abort();
        },
        onCancelled: () => {
          cancelled = true;
          aborter.abort();
        },
      });

      let committed;
      try {
        committed = await this.storage.put(
          videoKey(video.secUserId, video.awemeId),
          metered,
          // The verified truncation guard. `undefined` when the CDN advertised
          // nothing usable -- never a fabricated number, which would make the
          // check reject every download instead of protecting it.
          res.contentLength,
          // THE OWNERSHIP RE-CHECK, run between the last byte and the rename.
          // Cancellation is asked here too, and that is what makes "cancel
          // mid-download leaves no truncated file" true by construction rather
          // than by timing: the rename is the only commit point, so a cancel
          // that arrives before it cannot leave anything behind.
          async () => {
            // ZERO BYTES IS NEVER A VIDEO, and the length check above cannot
            // catch it: `0 !== 0` is false, so an empty body matching an empty
            // `Content-Length: 0` passes verification, gets fsync'd, renamed,
            // and marked `done`. `claimOne` never looks at a `done` row again,
            // so there would be no detector and no way back -- the archive just
            // holds an unplayable file while every count agrees. Asked here
            // rather than off `res.contentLength`, so the missing-header case is
            // covered by the same guard.
            if (transferred === 0) {
              empty = true;
              return false;
            }
            if (await jobsRepository.isProfileCancelled(this.db, video.secUserId)) {
              cancelled = true;
              return false;
            }
            if (!(await videosRepository.holdsClaim(this.db, video.awemeId, token))) {
              lost = true;
              return false;
            }
            return true;
          },
        );
      } finally {
        // Awaited, not merely cleared -- see `startHeartbeat`.
        await beat.stop();
        // The socket, if the put gave up before draining it. Left open it holds
        // a connection -- and, under jest, the process.
        res.body.destroy();
      }

      if (committed === null) {
        if (empty) {
          throw new Error(
            `cdn answered 200 for video ${video.awemeId} and then sent no bytes at all; ` +
              `refusing to commit a zero-length file under its name`,
          );
        }
        if (cancelled) return 'cancelled';
        this.logger.warn(
          `lane ${this.name}: declined to commit video ${video.awemeId}; this claim is no ` +
            `longer ours, so another worker owns the file now. Nothing was written.`,
        );
        return 'lost';
      }

      // Unconditional, and deliberately so -- see `markDone`'s own note. By
      // this point the bytes under that key are a COMPLETE copy of this video,
      // whoever wrote them, so `done` plus that path is the same truth for any
      // claim. A replacement still streaming will find the row no longer
      // `downloading` and decline its own commit, which is the right outcome.
      await videosRepository.markDone(this.db, video.awemeId, committed.path);
      this.logger.log(
        `lane ${this.name}: video ${video.awemeId} downloaded (${committed.bytes} bytes)`,
      );
      return 'committed';
    } catch (err: unknown) {
      if (lost) return 'lost';
      if (cancelled) return 'cancelled';
      // A shutdown mid-transfer is not a failure: the claim goes back through
      // `stop()`'s release, which is the whole point of holding it.
      if (signal.aborted) return 'interrupted';
      throw err;
    } finally {
      // One long-lived lane signal, thousands of downloads: a listener added
      // per call and never removed is an unbounded array on a process-lifetime
      // object. Same hygiene sleep.ts spells out.
      signal.removeEventListener('abort', onLaneAbort);
    }
  }

  /**
   * Renews the lease while bytes are moving, and notices when the claim or the
   * job has gone away.
   *
   * The risk register's "File lớn vượt lease giữa lúc tải". A 200MB video on a
   * slow CDN legitimately outlives `LEASE_SECONDS`, and the reaper cannot tell
   * that from a dead worker -- both are a `downloading` row with a lapsed
   * lease. So a live download says so.
   *
   * GATED ON PROGRESS. A renewal is only sent when more bytes have arrived
   * since the last beat, so a wedged socket cannot hold a claim open forever by
   * renewing a lease it is not earning. That is the same rule
   * `jobs.repository.ts`'s `checkpoint` follows, and for the same reason.
   *
   * `renewLease` returning false means the row is no longer this claim's -- the
   * reaper handed it over. Aborting immediately is better than downloading to
   * the end and being refused at the commit: those are bytes nobody will use.
   */
  private startHeartbeat(
    video: Video,
    transferred: () => number,
    on: { onLost: () => void; onCancelled: () => void },
  ): { stop: () => Promise<void> } {
    let lastSeen = 0;
    /**
     * `clearInterval` stops SCHEDULING; it does not stop the beat already in
     * flight. That beat is two database round trips, so it can land after the
     * commit -- and then `on.onLost()` fires for a download that is already
     * `done`, setting a flag the catch below reads FIRST. A `markDone` that
     * threw would then be answered as `'lost'`, skipping `markFailed` entirely
     * and parking a committed file's row in `downloading` for the reaper.
     *
     * So the beat is both flagged and awaited: `stopped` keeps a beat that has
     * not started yet from acting, and `inFlight` is what `stop()` waits on for
     * one that has.
     */
    let stopped = false;
    let inFlight: Promise<void> = Promise.resolve();
    const timer = setInterval(() => {
      const seen = transferred();
      if (seen === lastSeen) return;
      lastSeen = seen;
      // Fire-and-forget with its own catch: an unhandled rejection out of a
      // timer callback is a process-level crash, and this is a best-effort
      // renewal whose failure the commit check would catch anyway.
      inFlight = (async () => {
        if (stopped) return;
        if (await jobsRepository.isProfileCancelled(this.db, video.secUserId)) {
          if (stopped) return;
          this.logger.log(
            `lane ${this.name}: job for video ${video.awemeId} was cancelled; ` +
              `abandoning the transfer`,
          );
          on.onCancelled();
          return;
        }
        const stillOurs = await videosRepository.renewLease(
          this.db,
          video.awemeId,
          video.attempts,
          this.opts.leaseSeconds,
        );
        if (!stillOurs && !stopped) {
          this.logger.warn(
            `lane ${this.name}: lost the claim on video ${video.awemeId} mid-download; ` +
              `abandoning the transfer rather than finishing bytes nobody can commit`,
          );
          on.onLost();
        }
      })().catch((err: unknown) => {
        this.logger.error(
          `lane ${this.name}: heartbeat for video ${video.awemeId} failed: ${describe(err)}`,
        );
      });
    }, this.heartbeatMs);
    // Never a reason to hold the process open. An un-unref'd interval turns
    // "the work is finished" into a hang.
    timer.unref();
    return {
      stop: async () => {
        stopped = true;
        clearInterval(timer);
        await inFlight;
      },
    };
  }

  /**
   * F1's reactive re-resolve: one `fetch_one_video`, cached on the way past.
   *
   * THE COST OF DOING THIS UNCONDITIONALLY, which is what IDEA.md §3.2 asks
   * for: a 500-video profile needs ~25 requests to enumerate and would need
   * ~525 with a resolve per video. Douyin rate-limits by IP, so that is 3-5x
   * the hourly budget and a near-certain ban -- for information the enumerate
   * response already carried. Plan §7 records this as a DELIBERATE deviation.
   *
   * Note what is not being claimed: F1 does not trust the cached URL. It
   * declines to CHECK it in advance, and re-resolves the moment it stops
   * working (the 403/410 path above). If you are about to "fix" this to match
   * §3.2, the two numbers are 25 and 525.
   */
  private async resolve(awemeId: string, why: string): Promise<string> {
    this.logger.log(`lane ${this.name}: re-resolving play_addr for video ${awemeId} -- ${why}`);
    const { playAddrUrls } = await this.parser.fetchOneVideo(awemeId);
    const [url] = playAddrUrls;
    if (url === undefined) {
      throw new Error(
        `douyin returned no play_addr for video ${awemeId}; it may have been deleted or made ` +
          `private`,
      );
    }
    // Cached for whoever downloads this video next -- including a replacement
    // worker if this attempt is the one that gets reaped.
    await videosRepository.saveResolvedUrl(this.db, awemeId, url, this.opts.urlTtlSeconds);
    return url;
  }

  /**
   * §6.2 step 8 -- whichever lane finishes a profile's last video finalizes its
   * job.
   *
   * The cheap read first, then the write. `finalizeSettled`'s own predicate
   * already answers "is anything outstanding?", so this looks redundant -- it
   * is not: without it every one of a 500-video profile's completions takes a
   * row lock on the job row, six lanes deep, to discover there is nothing to
   * do 499 times. The read uses `videos_profile_recent_idx` and takes no locks.
   *
   * This check can LOSE, and that is expected: two lanes finishing the last two
   * videos can both see the other's row still outstanding and both correctly
   * decline, leaving the job at `downloading` with everything done. The reaper's
   * finalize sweep (phase 4, 60s) is the safety net for exactly that, which is
   * why this does not need to be a transaction.
   */
  private async settle(secUserId: string): Promise<void> {
    if (await videosRepository.hasWorkForProfile(this.db, secUserId)) return;
    const finalized = await jobsRepository.finalizeSettled(this.db, { secUserId });
    for (const job of finalized) {
      this.logger.log(
        `lane ${this.name}: job ${job.id} completed -- ${job.downloadedCount} downloaded, ` +
          `${job.failedCount} failed of ${job.totalVideos ?? 0}`,
      );
    }
  }
}

/** See `lane.runner.ts`'s note on why this is not `err.message`. */
function describe(err: unknown): string {
  if (err === null) return 'null';
  if (err === undefined) return 'undefined';
  if (typeof err === 'object') {
    const maybe = err as { message?: unknown };
    if (typeof maybe.message === 'string') return maybe.message;
    try {
      return JSON.stringify(err);
    } catch {
      return Object.prototype.toString.call(err);
    }
  }
  return String(err);
}
