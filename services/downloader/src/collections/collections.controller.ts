import { Body, Controller, Delete, Get, HttpCode, HttpStatus, Param, Patch, Post, Put } from '@nestjs/common';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import {
  collectionParamsSchema,
  createCollectionSchema,
  glossaryBodySchema,
  glossaryParamsSchema,
  updateCollectionSchema,
  type CreateCollectionBody,
  type GlossaryBody,
  type UpdateCollectionBody,
} from './collections.schemas';
import { CollectionsService } from './collections.service';
import { glossaryTermSchema, normalizeTerm, type GlossaryTerm } from './glossary-term';

const termPipe = new ZodValidationPipe(glossaryTermSchema);

/**
 * Collections and their glossary. `ApiKeyGuard` covers every route, as it covers
 * the whole application; that key ships in the web bundle, so it is not the
 * security boundary. The boundaries are the export root on the Mac and the term
 * guard (`checkTerm`) that a dashboard term passes like an agent term.
 */
@Controller('api/collections')
export class CollectionsController {
  constructor(private readonly collections: CollectionsService) {}

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
