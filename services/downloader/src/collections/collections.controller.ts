import {
  Body,
  Controller,
  Delete,
  Get,
  HttpCode,
  HttpStatus,
  Logger,
  Param,
  PayloadTooLargeException,
  Patch,
  Post,
  Put,
  Query,
  Req,
  Res,
  UnsupportedMediaTypeException,
} from '@nestjs/common';
import type { Request, Response } from 'express';
import { pipeline } from 'node:stream';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { CollectionWatermarksService, MAX_WATERMARK_PNG_BYTES } from './collection-watermarks';
import {
  collectionParamsSchema,
  createCollectionSchema,
  glossaryBodySchema,
  glossaryParamsSchema,
  updateCollectionSchema,
  watermarkMetaSchema,
  watermarkParamsSchema,
  type CreateCollectionBody,
  type GlossaryBody,
  type UpdateCollectionBody,
  type WatermarkMeta,
} from './collections.schemas';
import { CollectionsService } from './collections.service';
import { CoverPreviewService } from './cover-preview';
import { glossaryTermSchema, normalizeTerm, type GlossaryTerm } from './glossary-term';

const termPipe = new ZodValidationPipe(glossaryTermSchema);
const log = new Logger('CollectionWatermarks');

/**
 * The raw request body, refused past `limit` bytes. Only for a content type no body
 * parser handles (`image/png`), so the stream is still unread here.
 */
async function readBody(req: Request, limit: number): Promise<Buffer> {
  // Read to the end even when refused (nginx caps a request at 1 MiB): an answer sent
  // while the client is still writing reaches it as a reset connection.
  const chunks: Buffer[] = [];
  let size = 0;
  req.on('data', (chunk: Buffer) => {
    size += chunk.length;
    if (size <= limit) chunks.push(chunk);
  });
  await new Promise<void>((resolve, reject) => {
    req.once('end', resolve);
    req.once('error', reject);
  });
  if (!req.is('image/png')) throw new UnsupportedMediaTypeException('Body must be Content-Type: image/png');
  if (size > limit) throw new PayloadTooLargeException(`PNG must be at most ${limit} bytes`);
  return Buffer.concat(chunks);
}

/**
 * Collections and their glossary. `ApiKeyGuard` covers every route, as it covers
 * the whole application; that key ships in the web bundle, so it is not the
 * security boundary. The boundaries are the export root on the Mac and the term
 * guard (`checkTerm`) that a dashboard term passes like an agent term.
 */
@Controller('api/collections')
export class CollectionsController {
  constructor(
    private readonly collections: CollectionsService,
    private readonly coverPreview: CoverPreviewService,
    private readonly watermarks: CollectionWatermarksService,
  ) {}

  @Get()
  list() {
    return this.collections.list();
  }

  @Post()
  create(@Body(new ZodValidationPipe(createCollectionSchema)) body: CreateCollectionBody) {
    return this.collections.create(body);
  }

  @Patch(':id')
  update(
    @Param(new ZodValidationPipe(collectionParamsSchema)) params: { id: string },
    @Body(new ZodValidationPipe(updateCollectionSchema)) body: UpdateCollectionBody,
  ) {
    return this.collections.update(params.id, body);
  }

  /** Only an empty collection; 409 says what still uses it. */
  @Delete(':id')
  @HttpCode(HttpStatus.NO_CONTENT)
  remove(@Param(new ZodValidationPipe(collectionParamsSchema)) params: { id: string }) {
    return this.collections.remove(params.id);
  }

  /** A frame size, subtitle band and signed source link to preview the cover box on. */
  @Get(':id/cover-preview')
  preview(@Param(new ZodValidationPipe(collectionParamsSchema)) params: { id: string }) {
    return this.coverPreview.preview(params.id);
  }

  @Get(':id/watermarks')
  listWatermarks(@Param(new ZodValidationPipe(collectionParamsSchema)) params: { id: string }) {
    return this.watermarks.list(params.id);
  }

  /** The body is the cut PNG; the query says where it was cut (`watermarkMetaSchema`). */
  @Post(':id/watermarks')
  async addWatermark(
    @Param(new ZodValidationPipe(collectionParamsSchema)) params: { id: string },
    @Query(new ZodValidationPipe(watermarkMetaSchema)) meta: WatermarkMeta,
    @Req() req: Request,
  ) {
    const png = await readBody(req, MAX_WATERMARK_PNG_BYTES);
    return this.watermarks.add(params.id, meta, png);
  }

  @Get(':id/watermarks/:watermarkId/image')
  async watermarkImage(
    @Param(new ZodValidationPipe(watermarkParamsSchema)) params: { id: string; watermarkId: string },
    @Res() res: Response,
  ): Promise<void> {
    const object = await this.watermarks.image(params.id, params.watermarkId);
    res.status(200);
    // Always PNG: the upload was checked to be one, and a stored type never decides it.
    res.setHeader('Content-Type', 'image/png');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    // An image never changes under its id.
    res.setHeader('Cache-Control', 'private, max-age=86400, immutable');
    if (object.contentLength !== undefined) res.setHeader('Content-Length', String(object.contentLength));
    pipeline(object.body, res, (err) => {
      if (err && (err as { code?: string }).code !== 'ERR_STREAM_PREMATURE_CLOSE') {
        log.warn(`watermark image stream failed: ${err.name}: ${err.message}`);
      }
    });
  }

  @Delete(':id/watermarks/:watermarkId')
  @HttpCode(HttpStatus.NO_CONTENT)
  removeWatermark(@Param(new ZodValidationPipe(watermarkParamsSchema)) params: { id: string; watermarkId: string }) {
    return this.watermarks.remove(params.id, params.watermarkId);
  }

  @Get(':id/glossary')
  glossary(@Param(new ZodValidationPipe(collectionParamsSchema)) params: { id: string }) {
    return this.collections.glossary(params.id);
  }

  @Put(':id/glossary/:term_zh')
  putTerm(
    @Param(new ZodValidationPipe(glossaryParamsSchema)) params: { id: string; term_zh: string },
    @Body(new ZodValidationPipe(glossaryBodySchema)) body: GlossaryBody,
  ) {
    // The Chinese term comes from the path, so the term rules run once both halves are known.
    const term = termPipe.transform({ term_zh: params.term_zh, ...body }) as GlossaryTerm;
    return this.collections.putTerm(params.id, term);
  }

  @Delete(':id/glossary/:term_zh')
  @HttpCode(HttpStatus.NO_CONTENT)
  deleteTerm(@Param(new ZodValidationPipe(glossaryParamsSchema)) params: { id: string; term_zh: string }) {
    // Stored terms are stripped and NFC'd (here and by the worker), so the key is too.
    return this.collections.deleteTerm(params.id, normalizeTerm(params.term_zh));
  }
}
