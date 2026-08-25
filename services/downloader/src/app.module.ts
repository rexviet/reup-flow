import { Module } from "@nestjs/common";
import { APP_GUARD } from "@nestjs/core";
import { HealthController } from "./health/health.controller";
import { ConfigModule } from "./config/config.module";
import { DbModule } from "./db/db.module";
import { RedisModule } from "./redis/redis.module";
import { ApiKeyGuard } from "./auth/api-key.guard";
import { ParserModule } from "./parser/parser.module";
import { CdnModule } from "./cdn/cdn.module";
import { JobsModule } from "./jobs/jobs.module";
import { VideosModule } from "./videos/videos.module";
import { CookieModule } from "./cookie/cookie.module";
import { StorageModule } from "./storage/storage.module";
import { LanesModule } from "./lanes/lanes.module";

/**
 * `ApiKeyGuard` is registered here as `APP_GUARD` -- the one place that
 * applies it to every controller in the application, `/api` and
 * `/internal` alike (phase-3 DoD), with no way for a future controller to
 * be added and forget to protect itself. `@Public()` is the only opt-out,
 * and only `HealthController` uses it.
 *
 * ConfigModule/DbModule/RedisModule/ParserModule/CdnModule/StorageModule are all
 * `@Global()`; importing them here once is what makes `ENV`/`DB`/`PG_POOL`/
 * `REDIS`/`PARSER_CLIENT`/`CDN_CLIENT`/`STORAGE` available to every feature module
 * (jobs/videos/cookie) and to the lanes without each of them re-importing
 * these explicitly.
 *
 * This is also the ONE place the HTTP layer and the lane layer meet -- and
 * they meet only by being siblings here. `LanesModule` and the
 * jobs/videos/cookie modules never import each other (plan §0 principle 1);
 * Postgres is the only channel between them. `.eslintrc.json` enforces that
 * at lint time and `test/architecture/http-lane-boundary.spec.ts` enforces it
 * at test time, including the dynamic-import and computed-`require` bypasses
 * lint cannot see.
 *
 * Whether the lanes in `LanesModule` actually RUN in this process is
 * `LANES_ENABLED`'s decision, made by `LaneSupervisor` at bootstrap -- see
 * env.ts. Importing the module is not the same as starting the loops.
 */
@Module({
  imports: [
    ConfigModule,
    DbModule,
    RedisModule,
    ParserModule,
    CdnModule,
    StorageModule,
    LanesModule,
    JobsModule,
    VideosModule,
    CookieModule,
  ],
  controllers: [HealthController],
  providers: [{ provide: APP_GUARD, useClass: ApiKeyGuard }],
})
export class AppModule {}
