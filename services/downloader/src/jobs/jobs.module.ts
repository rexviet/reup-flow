import { Module } from '@nestjs/common';
import { JobsController } from './jobs.controller';
import { JobsService } from './jobs.service';

/**
 * `DB` and `PARSER_CLIENT` are not provided here -- `DbModule` and
 * `ParserModule` are both `@Global()` and already imported once in
 * `app.module.ts`, which is what makes them available to `JobsService`'s
 * constructor without this module re-importing either.
 */
@Module({
  controllers: [JobsController],
  providers: [JobsService],
})
export class JobsModule {}
