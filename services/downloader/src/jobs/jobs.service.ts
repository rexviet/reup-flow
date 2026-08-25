import { Inject, Injectable } from '@nestjs/common';
import type { NodePgDatabase } from 'drizzle-orm/node-postgres';
import { DB } from '../db/db.module';
import * as jobsRepository from '../db/jobs.repository';
import type { Page, PageOptions } from '../db/jobs.repository';
import * as schema from '../db/schema';
import type { Job } from '../db/schema';
import * as videosRepository from '../db/videos.repository';
import { PARSER_CLIENT } from '../parser/parser.module';
import type { ParserClient } from '../parser/parser.client';

export interface RetryResult {
  job: Job;
  videosReset: number;
}

@Injectable()
export class JobsService {
  constructor(
    @Inject(DB) private readonly db: NodePgDatabase<typeof schema>,
    @Inject(PARSER_CLIENT) private readonly parser: ParserClient,
  ) {}

  /**
   * Plan §0 principle 1: the HTTP layer never calls a lane. This method does
   * exactly three things -- resolve the URL, upsert the profile, insert a
   * `pending` job -- and then returns. It does NOT call, schedule, notify,
   * or import anything under `lanes/`; the enumerate lane (phase 5) finds
   * this row entirely on its own next poll. Postgres is the only channel
   * between this method and the lane, which is what makes moving lanes into
   * their own process later cost zero logic changes here.
   *
   * The parser call happens BEFORE the transaction starts on purpose: it is
   * a slow network round trip, and holding a Postgres transaction open across
   * it would tie up a connection (and, if a `SKIP LOCKED` claim were sharing
   * that transaction, a lock) for no reason -- there is nothing to roll back
   * about a read that talks to nothing but the parser.
   */
  async createFromUrl(url: string): Promise<Job> {
    const secUserId = await this.parser.resolveSecUserId(url);
    return this.db.transaction(async (tx) => {
      await jobsRepository.upsertProfile(tx, secUserId);
      const job = await jobsRepository.create(tx, secUserId);
      // PHASE 7 -- somebody is asking for this profile again, so nobody's
      // earlier cancel applies to it any more.
      //
      // A cancel has the download lane mark the profile's outstanding videos
      // `skipped`, and until phase 7 nothing ever reset that. This new job would
      // re-enumerate them, `upsertMany`'s `ON CONFLICT DO NOTHING` would
      // correctly do nothing (F7), and `claimOne` -- which only ever looks at
      // `pending` -- would never see them. The job would then find no work; and
      // because `skipped` counts as SETTLED, `finalizeSettled` would walk it to
      // `completed` with `downloaded_count = 0` and no error anywhere. Every
      // number would agree and the videos would simply never arrive.
      //
      // In the SAME transaction as the insert, so there is no window in which a
      // live job exists beside videos still marked skipped -- a lane ticking in
      // that window would find nothing to do and could finalize the job it was
      // created to run.
      await videosRepository.reviveSkippedForProfile(tx, secUserId);
      return job;
    });
  }

  async list(opts: PageOptions): Promise<Page<Job>> {
    return jobsRepository.list(this.db, opts);
  }

  async findById(id: string): Promise<Job | null> {
    return jobsRepository.findById(this.db, id);
  }

  async cancel(id: string): Promise<Job | null> {
    return jobsRepository.cancel(this.db, id);
  }

  /**
   * `/retry`: three different tables' concerns, composed here and nowhere else.
   *
   *   - `jobsRepository.retry` -- the job: blocked|failed|cancelled -> pending.
   *   - `resetFailedForProfile` -- videos that were TRIED and could not be got.
   *   - `reviveSkippedForProfile` -- videos a cancel passed over (phase 7).
   *
   * The last two are separate calls rather than one widened UPDATE because
   * `failed` and `skipped` mean opposite things -- "we tried and could not" vs
   * "nothing went wrong, nobody wanted it" -- and a single function would end up
   * with a name true of half of what it does. See the note on
   * `reviveSkippedForProfile`.
   *
   * ONE transaction, so a job that comes back reporting "retried" always has its
   * videos reset along with it. The reverse -- a retried job whose videos stayed
   * skipped -- is worse than an error: `skipped` counts as settled, so the job
   * would immediately finalize as `completed` with nothing downloaded.
   *
   * BOTH counts are summed into `videosReset` because that number is what the
   * API answers, and it is only useful as confirmation if it covers everything
   * the call actually did. A purely-cancelled profile reporting "0 reset" having
   * just revived its whole backlog would be the opposite of confirmation.
   *
   * Gated on the retry landing: an unconditional revival would un-skip a profile
   * whose cancel is still current, and the next lane tick would re-skip it --
   * spending an attempt per pass, forever.
   */
  async retry(id: string): Promise<RetryResult | null> {
    return this.db.transaction(async (tx) => {
      const job = await jobsRepository.retry(tx, id);
      if (!job) return null;
      const failedReset = await videosRepository.resetFailedForProfile(tx, job.secUserId);
      const skippedRevived = await videosRepository.reviveSkippedForProfile(tx, job.secUserId);
      return { job, videosReset: failedReset + skippedRevived };
    });
  }

  /** F7 -- see the long comment on `countByStatusForProfile` in
   * videos.repository.ts for why this is scoped to the profile, not the job. */
  async profileVideoCounts(secUserId: string): Promise<Record<string, number>> {
    return videosRepository.countByStatusForProfile(this.db, secUserId);
  }
}
