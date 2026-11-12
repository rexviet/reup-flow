import { Body, Controller, HttpCode, HttpStatus, Post } from '@nestjs/common';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { DashboardActionsService } from './dashboard.actions';
import { actionBodySchema, type ActionBody } from './dashboard.schemas';

@Controller('api/dashboard')
export class DashboardActionsController {
  constructor(private readonly actions: DashboardActionsService) {}

  /** One route for every action, single or bulk (1 to 100 videos). */
  @Post('actions')
  @HttpCode(HttpStatus.OK)
  run(@Body(new ZodValidationPipe(actionBodySchema)) body: ActionBody) {
    return this.actions.run(body);
  }
}
