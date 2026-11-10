import { Controller, Get, Query, ServiceUnavailableException } from '@nestjs/common';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { ReviewsRepository } from './reviews.repository';
import { reviewQuerySchema, type ReviewList, type ReviewQuery } from './reviews.schemas';

@Controller('api/reviews')
export class ReviewsController {
  constructor(private readonly reviews: ReviewsRepository) {}

  @Get()
  async list(@Query(new ZodValidationPipe(reviewQuerySchema)) query: ReviewQuery): Promise<ReviewList> {
    try {
      return await this.reviews.list(query);
    } catch {
      throw new ServiceUnavailableException('Review discovery unavailable; check database migration and connectivity');
    }
  }
}
