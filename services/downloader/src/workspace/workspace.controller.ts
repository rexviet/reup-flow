import { Controller, Get, NotFoundException, Param, ServiceUnavailableException } from '@nestjs/common';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { RunLookupError, WorkspaceService } from './workspace.service';
import { runParamsSchema, type RunDetail, type Snapshot } from './workspace.schemas';

/**
 * Read-only. `ApiKeyGuard` covers it as it covers every route (APP_GUARD in
 * app.module.ts). It answers 200 even when Temporal or Postgres is down; the
 * body says which source is.
 */
@Controller('api/workspace')
export class WorkspaceController {
  constructor(private readonly workspace: WorkspaceService) {}

  @Get('snapshot')
  snapshot(): Promise<Snapshot> {
    return this.workspace.snapshot();
  }

  @Get('runs/:awemeId/:generation')
  async run(
    @Param(new ZodValidationPipe(runParamsSchema)) params: { awemeId: string; generation: number },
  ): Promise<RunDetail> {
    try {
      const detail = await this.workspace.runDetail(params);
      if (!detail) throw new NotFoundException(`run ${params.awemeId}/${params.generation} not found`);
      return detail;
    } catch (err) {
      if (err instanceof RunLookupError) throw new ServiceUnavailableException(err.message);
      throw err;
    }
  }
}
