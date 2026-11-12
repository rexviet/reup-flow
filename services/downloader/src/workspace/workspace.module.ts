import { Module } from '@nestjs/common';
import type { Client } from '@temporalio/client';
import { TEMPORAL_CLIENT } from '../temporal/temporal.module';
import { WorkspaceController } from './workspace.controller';
import { WorkspaceRepository } from './workspace.repository';
import { WorkspaceService } from './workspace.service';
import { DASHBOARD_TEMPORAL, TEMPORAL_READER } from './workspace.tokens';
import { TemporalDashboardReader } from './workspace.dashboard-reader';
import { TemporalWorkflowReader } from './workspace.temporal';

/** `PG_POOL` and `TEMPORAL_CLIENT` come from the global DbModule and TemporalModule. */
@Module({
  controllers: [WorkspaceController],
  providers: [
    WorkspaceRepository,
    { provide: DASHBOARD_TEMPORAL, inject: [TEMPORAL_CLIENT], useFactory: (c: Client) => new TemporalDashboardReader(c) },
    { provide: TEMPORAL_READER, inject: [TEMPORAL_CLIENT], useFactory: (c: Client) => new TemporalWorkflowReader(c) },
    {
      provide: WorkspaceService,
      inject: [TEMPORAL_READER, WorkspaceRepository],
      useFactory: (t: TemporalWorkflowReader, s: WorkspaceRepository) => new WorkspaceService(t, s),
    },
  ],
  exports: [DASHBOARD_TEMPORAL],
})
export class WorkspaceModule {}
