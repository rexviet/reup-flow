import { Module } from '@nestjs/common';
import { VideoEnqueueController } from './video-enqueue.controller';
import { VideosController } from './videos.controller';
import { VideosService } from './videos.service';

/** `DB` comes from the globally-imported `DbModule` -- see jobs.module.ts's
 * comment for why nothing is re-provided here. */
@Module({
  controllers: [VideosController, VideoEnqueueController],
  providers: [VideosService],
})
export class VideosModule {}
