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
      return jobsRepository.create(tx, secUserId);
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
   * `/retry`: `jobs.repository.ts`'s `retry` (job: blocked|failed -> pending)
   * and `videos.repository.ts`'s `resetFailedForProfile` (that profile's
   * failed videos: attempts -> 0) are two different tables' concerns, and
   * this is the one place that composes them -- in one transaction, so a
   * job that comes back reporting "retried" always has its videos reset
   * along with it, never one without the other.
   */
  async retry(id: string): Promise<RetryResult | null> {
    return this.db.transaction(async (tx) => {
      const job = await jobsRepository.retry(tx, id);
      if (!job) return null;
      const videosReset = await videosRepository.resetFailedForProfile(tx, job.secUserId);
      return { job, videosReset };
    });
  }

  /** F7 -- see the long comment on `countByStatusForProfile` in
   * videos.repository.ts for why this is scoped to the profile, not the job. */
  async profileVideoCounts(secUserId: string): Promise<Record<string, number>> {
    return videosRepository.countByStatusForProfile(this.db, secUserId);
  }
}
