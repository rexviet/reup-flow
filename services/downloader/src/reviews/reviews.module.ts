import { Module } from '@nestjs/common';
import { ReviewsController } from './reviews.controller';
import { ReviewsRepository } from './reviews.repository';

@Module({ controllers: [ReviewsController], providers: [ReviewsRepository] })
export class ReviewsModule {}
