import { Module } from "@nestjs/common";
import { APP_GUARD } from "@nestjs/core";
import { HealthController } from "./health/health.controller";
import { ConfigModule } from "./config/config.module";
import { DbModule } from "./db/db.module";
import { RedisModule } from "./redis/redis.module";
import { ApiKeyGuard } from "./auth/api-key.guard";
import { ParserModule } from "./parser/parser.module";
import { JobsModule } from "./jobs/jobs.module";
import { VideosModule } from "./videos/videos.module";
import { CookieModule } from "./cookie/cookie.module";

/**
 * `ApiKeyGuard` is registered here as `APP_GUARD` -- the one place that
 * applies it to every controller in the application, `/api` and
 * `/internal` alike (phase-3 DoD), with no way for a future controller to
 * be added and forget to protect itself. `@Public()` is the only opt-out,
 * and only `HealthController` uses it.
 *
 * ConfigModule/DbModule/RedisModule/ParserModule are all `@Global()`;
 * importing them here once is what makes `ENV`/`DB`/`PG_POOL`/`REDIS`/
 * `PARSER_CLIENT` available to every feature module (jobs/videos/cookie)
 * without each of them re-importing these explicitly.
 */
@Module({
  imports: [
    ConfigModule,
    DbModule,
    RedisModule,
    ParserModule,
    JobsModule,
    VideosModule,
    CookieModule,
  ],
  controllers: [HealthController],
  providers: [{ provide: APP_GUARD, useClass: ApiKeyGuard }],
})
export class AppModule {}
