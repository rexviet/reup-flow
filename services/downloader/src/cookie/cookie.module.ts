import { Module } from '@nestjs/common';
import { CookieController } from './cookie.controller';
import { CookieService } from './cookie.service';

/**
 * `REDIS` is provided by the `@Global()` `RedisModule` (imported once in
 * `AppModule`), so this module does not import it directly -- see
 * `redis.module.ts`'s own comment on why it is global. Group A's
 * `RedisModule`/`ConfigModule` are assumed already wired by the orchestrator;
 * this module only ever adds `CookieController`/`CookieService` to whatever
 * graph imports it.
 */
@Module({
  controllers: [CookieController],
  providers: [CookieService],
})
export class CookieModule {}
