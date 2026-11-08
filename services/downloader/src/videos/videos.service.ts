import { BadGatewayException, Inject, Injectable, NotFoundException } from '@nestjs/common';
import type { NodePgDatabase } from 'drizzle-orm/node-postgres';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { DB } from '../db/db.module';
import * as jobsRepository from '../db/jobs.repository';
import * as schema from '../db/schema';
import * as videosRepository from '../db/videos.repository';
import { toVideoUpsert } from '../parser/aweme.mapper';
import type { ParserClient } from '../parser/parser.client';
import { PARSER_CLIENT } from '../parser/parser.module';
import { extractAwemeId } from './video-url';

export interface EnqueuedVideo {
  awemeId: string;
  status: string;
}

@Injectable()
export class VideosService {
  constructor(
    @Inject(DB) private readonly db: NodePgDatabase<typeof schema>,
    @Inject(PARSER_CLIENT) private readonly parser: ParserClient,
    @Inject(ENV) private readonly env: Pick<Env, 'URL_TTL_SECONDS'>,
  ) {}

  /**
   * Queues ONE video for the download lane, without a job.
   *
   * Like `JobsService.createFromUrl` this only writes rows -- the download
   * lane finds the `pending` row on its own next poll, and nothing here
   * imports `lanes/`. The row is built by the same `toVideoUpsert` the
   * enumerate lane uses, so an image post is recorded `skipped` here too
   * instead of being queued to download an MP3 as `.mp4`.
   *
   * `upsertMany` is `ON CONFLICT DO NOTHING` (F7), which is what makes a
   * second click on an already-downloaded video a no-op: the answer is
   * whatever status the existing row already has.
   *
   * The parser call happens before any write, for the same reason as in
   * `createFromUrl`: no transaction is held open across a network round trip.
   */
  async enqueueFromUrl(url: string): Promise<EnqueuedVideo> {
    const awemeId = extractAwemeId(url);
    if (awemeId === null) throw new BadGatewayException('url does not name a video');

    const aweme = await this.parser.fetchAwemeDetail(awemeId);
    if (aweme === null) throw new NotFoundException(`video ${awemeId} not found`);

    const secUserId = authorSecUid(aweme);
    const row = secUserId === null ? null : toVideoUpsert(aweme, secUserId);
    // The id Douyin sent must be the one that was asked for: the row is keyed
    // on it, and answering for a different video than the caller clicked on
    // would be a silent wrong answer.
    if (row === null || row.awemeId !== awemeId) {
      throw new BadGatewayException(`parser returned an unusable record for video ${awemeId}`);
    }

    await this.db.transaction(async (tx) => {
      await jobsRepository.upsertProfile(tx, row.secUserId);
      await videosRepository.upsertMany(tx, [row], { urlTtlSeconds: this.env.URL_TTL_SECONDS });
    });

    const stored = await videosRepository.findByAwemeId(this.db, awemeId);
    return { awemeId, status: stored?.status ?? row.status ?? 'pending' };
  }
}

function authorSecUid(aweme: Record<string, unknown>): string | null {
  const author = aweme.author;
  if (typeof author !== 'object' || author === null) return null;
  const secUid = (author as Record<string, unknown>).sec_uid;
  return typeof secUid === 'string' && secUid.length > 0 ? secUid : null;
}
