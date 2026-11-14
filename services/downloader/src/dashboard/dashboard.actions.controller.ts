import { Body, Controller, HttpCode, HttpStatus, Patch, Post } from '@nestjs/common';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { DashboardActionsService } from './dashboard.actions';
import {
  actionBodySchema,
  moveCollectionBodySchema,
  type ActionBody,
  type MoveCollectionBody,
} from './dashboard.schemas';

@Controller('api/dashboard')
export class DashboardActionsController {
  constructor(private readonly actions: DashboardActionsService) {}

  /** One route for every action, single or bulk (1 to 100 videos). */
  @Post('actions')
  @HttpCode(HttpStatus.OK)
  run(@Body(new ZodValidationPipe(actionBodySchema)) body: ActionBody) {
    return this.actions.run(body);
  }

  /** Puts 1 to 100 videos in a collection by hand; 404 when the collection does not exist. */
  @Patch('videos/collection')
  moveCollection(@Body(new ZodValidationPipe(moveCollectionBodySchema)) body: MoveCollectionBody) {
    return this.actions.moveCollection(body);
  }
}
