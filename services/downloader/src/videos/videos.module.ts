import { Module } from '@nestjs/common';
import { VideosController } from './videos.controller';

/** `DB` comes from the globally-imported `DbModule` -- see jobs.module.ts's
 * comment for why nothing is re-provided here. */
@Module({
  controllers: [VideosController],
})
export class VideosModule {}
