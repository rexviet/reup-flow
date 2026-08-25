import { Controller, Get, Inject, Param, Query } from '@nestjs/common';
import type { NodePgDatabase } from 'drizzle-orm/node-postgres';
import { DB } from '../db/db.module';
import * as schema from '../db/schema';
import { listByProfile } from '../db/videos.repository';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { toVideoResponse } from './videos.mapper';
import { videoListQuerySchema } from './videos.schemas';
import type { VideoListQueryDto } from './videos.schemas';

/**
 * Browse-only: this controller never writes anything (plan §0 principle 1
 * applies trivially here -- there is no lane-adjacent write to accidentally
 * make). One route, matching phase-03 §3.1 exactly.
 */
@Controller('api/profiles')
export class VideosController {
  constructor(@Inject(DB) private readonly db: NodePgDatabase<typeof schema>) {}

  @Get(':secUserId/videos')
  async list(
    @Param('secUserId') secUserId: string,
    @Query(new ZodValidationPipe(videoListQuerySchema)) query: VideoListQueryDto,
  ): Promise<{ items: ReturnType<typeof toVideoResponse>[]; total: number; limit: number; offset: number }> {
    const { items, total } = await listByProfile(this.db, secUserId, query);
    return { items: items.map(toVideoResponse), total, limit: query.limit, offset: query.offset };
  }
}
