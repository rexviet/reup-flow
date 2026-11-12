import { Body, Controller, HttpCode, HttpStatus, Post } from '@nestjs/common';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { mapParserError } from '../parser/map-parser-error';
import { VideosService } from './videos.service';
import { createVideoSchema } from './videos.schemas';
import type { CreateVideoDto } from './videos.schemas';

/**
 * Queue one video by its page URL. 202, not 201: the row is created, but the
 * download happens later, in a lane this controller knows nothing about.
 */
@Controller('api/videos')
export class VideoEnqueueController {
  constructor(private readonly videosService: VideosService) {}

  @Post()
  @HttpCode(HttpStatus.ACCEPTED)
  async enqueue(
    @Body(new ZodValidationPipe(createVideoSchema)) body: CreateVideoDto,
  ): Promise<{ aweme_id: string; status: string }> {
    try {
      const { awemeId, status } = await this.videosService.enqueueFromUrl(body.url, body.localize);
      return { aweme_id: awemeId, status };
    } catch (err) {
      throw mapParserError(err);
    }
  }
}
