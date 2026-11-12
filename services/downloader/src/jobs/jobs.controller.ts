import {
  Body,
  ConflictException,
  Controller,
  Get,
  HttpCode,
  HttpStatus,
  NotFoundException,
  Param,
  Post,
  Query,
} from '@nestjs/common';
import { JobsService } from './jobs.service';
import { toJobDetailResponse, toJobResponse } from './jobs.mapper';
import { createJobSchema, paginationSchema, uuidParamSchema } from './jobs.schemas';
import type { CreateJobDto, PaginationDto } from './jobs.schemas';
import { ZodValidationPipe } from './zod-validation.pipe';
import { mapParserError } from '../parser/map-parser-error';

/**
 * Explicit paths, no `@Controller('api/jobs')` shortcut ambiguity: every
 * route here is what phase-03 §3.1 lists, byte for byte.
 *
 * `ApiKeyGuard` is NOT applied here -- it is registered once, globally, as
 * `APP_GUARD` in app.module.ts, which is what guarantees no future route
 * added to this controller can forget to protect itself.
 */
@Controller('api/jobs')
export class JobsController {
  constructor(private readonly jobsService: JobsService) {}

  /**
   * Plan §0 principle 1, enforced at this boundary too: this handler calls
   * `jobsService.createFromUrl` and returns whatever it returns. It does not
   * (and, by the ESLint `no-restricted-imports` rule plus the architecture
   * test in test/architecture/, cannot) reach into `lanes/` itself.
   */
  @Post()
  @HttpCode(HttpStatus.CREATED)
  async create(@Body(new ZodValidationPipe(createJobSchema)) body: CreateJobDto): Promise<{ job_id: string }> {
    try {
      const job = await this.jobsService.createFromUrl(body.url, body.auto_localize);
      return { job_id: job.id };
    } catch (err) {
      throw mapParserError(err);
    }
  }

  @Get()
  async list(
    @Query(new ZodValidationPipe(paginationSchema)) query: PaginationDto,
  ): Promise<{ items: ReturnType<typeof toJobResponse>[]; total: number; limit: number; offset: number }> {
    const { items, total } = await this.jobsService.list(query);
    return { items: items.map(toJobResponse), total, limit: query.limit, offset: query.offset };
  }

  @Get(':id')
  async findOne(
    @Param('id', new ZodValidationPipe(uuidParamSchema)) id: string,
  ): Promise<ReturnType<typeof toJobDetailResponse>> {
    const job = await this.jobsService.findById(id);
    if (!job) throw new NotFoundException(`job ${id} not found`);
    // F7 -- profile-wide counts, not a per-job delta; see the mapper and
    // countByStatusForProfile in videos.repository.ts.
    const counts = await this.jobsService.profileVideoCounts(job.secUserId);
    return toJobDetailResponse(job, counts);
  }

  @Post(':id/cancel')
  @HttpCode(HttpStatus.OK)
  async cancel(
    @Param('id', new ZodValidationPipe(uuidParamSchema)) id: string,
  ): Promise<ReturnType<typeof toJobResponse>> {
    const cancelled = await this.jobsService.cancel(id);
    if (cancelled) return toJobResponse(cancelled);
    // `cancel` returning null means either "no such job" or "wrong state" --
    // ONE extra findById is what tells them apart, and its result is reused
    // below rather than re-fetched. FIX 11 (review): this used to call
    // findById a SECOND time to build the 409 message and read its result
    // through a non-null assertion (`existing!.status`) -- a row that
    // vanished between the two reads (a concurrent operation racing this
    // same job) made that assertion lie, throwing a raw TypeError that Nest
    // reports as an unhandled 500 instead of either domain exception below.
    const existing = await this.jobsService.findById(id);
    if (!existing) throw new NotFoundException(`job ${id} not found`);
    throw new ConflictException(`job ${id} is not in a cancellable state (status=${existing.status})`);
  }

  @Post(':id/retry')
  @HttpCode(HttpStatus.OK)
  async retry(
    @Param('id', new ZodValidationPipe(uuidParamSchema)) id: string,
  ): Promise<{ job: ReturnType<typeof toJobResponse>; videos_reset: number }> {
    const result = await this.jobsService.retry(id);
    if (result) return { job: toJobResponse(result.job), videos_reset: result.videosReset };
    // See the identical comment on `cancel` above -- same fix, same reason.
    const existing = await this.jobsService.findById(id);
    if (!existing) throw new NotFoundException(`job ${id} not found`);
    throw new ConflictException(`job ${id} is not in a retryable state (status=${existing.status})`);
  }
}
