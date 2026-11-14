import { Module } from '@nestjs/common';
import type { DashboardTemporal } from '../workspace/workspace.dashboard-reader';
import { DASHBOARD_TEMPORAL } from '../workspace/workspace.tokens';
import { WorkspaceModule } from '../workspace/workspace.module';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { CookieService } from '../cookie/cookie.service';
import { AutoQueueController } from './auto-queue.controller';
import { DashboardActionsController } from './dashboard.actions.controller';
import { DashboardActionsService } from './dashboard.actions';
import type { Pool } from 'pg';
import { PG_POOL } from '../db/db.module';
import { createS3MediaReader, DashboardMediaService, MEDIA_READER, type MediaReader } from './dashboard.media';
import { DashboardMediaController } from './dashboard.media.controller';
import { DashboardController } from './dashboard.controller';
import { DashboardHealthService } from './dashboard.health.service';
import { DashboardRepository } from './dashboard.repository';
import { DashboardService } from './dashboard.service';
import { ProfilesController } from './profiles.controller';

/**
 * The read side of the dashboard. Temporal reaches it only through the reader
 * `WorkspaceModule` exports, so nothing here imports the Temporal SDK.
 * `ENV`, `PG_POOL`, `REDIS` and the task-queue probe come from global modules.
 */
@Module({
  imports: [WorkspaceModule],
  controllers: [
    DashboardController,
    DashboardActionsController,
    DashboardMediaController,
    ProfilesController,
    AutoQueueController,
  ],
  providers: [
    DashboardRepository,
    CookieService,
    DashboardHealthService,
    DashboardActionsService,
    {
      provide: DashboardMediaService,
      inject: [PG_POOL, ENV, MEDIA_READER],
      useFactory: (pool: Pool, env: Env, reader: MediaReader | null) =>
        new DashboardMediaService(pool, env, reader, Date.now),
    },
    { provide: MEDIA_READER, inject: [ENV], useFactory: createS3MediaReader },
    {
      provide: DashboardService,
      inject: [DashboardRepository, DASHBOARD_TEMPORAL, ENV],
      useFactory: (repo: DashboardRepository, temporal: DashboardTemporal, env: Env) =>
        new DashboardService(repo, temporal, env.DASHBOARD_STUCK_MINUTES),
    },
  ],
  // The collection cover preview reads review manifests and signs source links with these.
  exports: [DashboardMediaService, MEDIA_READER],
})
export class DashboardModule {}
