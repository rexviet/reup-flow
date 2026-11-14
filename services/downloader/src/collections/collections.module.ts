import { Module } from '@nestjs/common';
import { CollectionsController } from './collections.controller';
import { CollectionsService } from './collections.service';

/** Collections and their glossary. `PG_POOL` comes from the global `DbModule`. */
@Module({
  controllers: [CollectionsController],
  providers: [CollectionsService],
})
export class CollectionsModule {}
