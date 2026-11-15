import { Controller, Get, Param, Query } from '@nestjs/common';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { DashboardHealthService } from './dashboard.health.service';
import { awemeParamsSchema, videosQuerySchema, type VideosQuery } from './dashboard.schemas';
import { DashboardService } from './dashboard.service';

/**
 * Read only. `ApiKeyGuard` covers it as it covers every route. Each route answers
 * 200 when Temporal or Postgres is down; the body's `sources` says which one.
 */
@Controller('api/dashboard')
export class DashboardController {
  constructor(
    private readonly dashboard: DashboardService,
    private readonly health: DashboardHealthService,
  ) {}

  @Get('videos')
  videos(@Query(new ZodValidationPipe(videosQuerySchema)) query: VideosQuery) {
    return this.dashboard.list(query);
  }

  @Get('videos/:awemeId')
  video(@Param(new ZodValidationPipe(awemeParamsSchema)) params: { awemeId: string }) {
    return this.dashboard.detail(params.awemeId);
  }

  @Get('attention')
  attention() {
    return this.dashboard.attention();
  }

  @Get('channels')
  channels() {
    return this.dashboard.channels();
  }

  @Get('health')
  healthReport() {
    return this.health.report();
  }
}
