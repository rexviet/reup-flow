import { Body, Controller, Logger, Get, Headers, HttpCode, HttpStatus, Param, Post, Query, Res } from '@nestjs/common';
import type { Response } from 'express';
import { pipeline } from 'node:stream';
import { SignedMedia } from '../auth/signed-media.decorator';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { DashboardMediaService } from './dashboard.media';
import { mediaLinksSchema, mediaQuerySchema, type MediaLinksBody } from './dashboard.schemas';

const log = new Logger('DashboardMedia');

@Controller('api/dashboard')
export class DashboardMediaController {
  constructor(private readonly media: DashboardMediaService) {}

  /** Needs the API key like every route; hands back short-lived signed URLs. */
  @Post('media-links')
  @HttpCode(HttpStatus.OK)
  links(@Body(new ZodValidationPipe(mediaLinksSchema)) body: MediaLinksBody) {
    return this.media.links(body.aweme_id, body.generation, body.kinds);
  }

  /** Authorized by the signature in the URL instead of the API key; see `SignedMedia`. */
  @Get('media/:token')
  @SignedMedia()
  async stream(
    @Param('token') token: string,
    @Query(new ZodValidationPipe(mediaQuerySchema)) query: { download?: boolean },
    @Headers('range') range: string | undefined,
    @Res() res: Response,
  ): Promise<void> {
    const { object, filename, contentType } = await this.media.open(token, range);
    res.status(object.status);
    res.setHeader('Content-Type', contentType);
    res.setHeader('Accept-Ranges', 'bytes');
    res.setHeader('Cache-Control', 'private, no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (object.contentLength !== undefined) res.setHeader('Content-Length', String(object.contentLength));
    if (object.contentRange) res.setHeader('Content-Range', object.contentRange);
    if (query.download) res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
    // Streams chunk by chunk; a closed tab destroys the S3 body instead of leaving it open.
    pipeline(object.body, res, (err) => {
      // After the headers went out the only signal left is a truncated body, so log it (no token).
      if (err && (err as { code?: string }).code !== 'ERR_STREAM_PREMATURE_CLOSE') {
        log.warn(`media stream failed: ${err.name}: ${err.message}`);
      }
    });
  }
}
