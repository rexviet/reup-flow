import { Module } from '@nestjs/common';
import { DashboardModule } from '../dashboard/dashboard.module';
import { CollectionsController } from './collections.controller';
import { CollectionsService } from './collections.service';
import { CoverPreviewService } from './cover-preview';

/**
 * Collections and their glossary. `PG_POOL` comes from the global `DbModule`; the
 * media reader and link signer for the cover preview come from `DashboardModule`.
 */
@Module({
  imports: [DashboardModule],
  controllers: [CollectionsController],
  providers: [CollectionsService, CoverPreviewService],
})
export class CollectionsModule {}
