import { Global, Inject, Module, type OnApplicationShutdown } from '@nestjs/common';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { ParserClient } from './parser.client';

/** Injection token for the shared `ParserClient`. */
export const PARSER_CLIENT = 'PARSER_CLIENT';

/**
 * @Global() so jobs/videos (and any future feature module) can `@Inject
 * (PARSER_CLIENT)` without importing this module explicitly everywhere --
 * same shape as `DbModule`/`RedisModule`.
 *
 * `OnApplicationShutdown` calls `client.close()`. The client owns a private undici
 * `Agent` (see parser.client.ts) precisely so it has something to close: a
 * successful call leaves a keep-alive socket on the event loop, and a
 * process holding one of those does not exit on its own. Phase 4's graceful
 * shutdown depends on this running -- without it, `docker stop` would have
 * to wait out Node's default force-kill timeout instead of the process
 * exiting cleanly.
 */
@Global()
@Module({
  providers: [
    {
      provide: PARSER_CLIENT,
      inject: [ENV],
      useFactory: (env: Env): ParserClient => new ParserClient({ baseUrl: env.PARSER_BASE_URL }),
    },
  ],
  exports: [PARSER_CLIENT],
})
export class ParserModule implements OnApplicationShutdown {
  constructor(@Inject(PARSER_CLIENT) private readonly client: ParserClient) {}

  /**
   * WHY `OnApplicationShutdown` AND NOT `OnModuleDestroy` (phase 4 §4.2).
   *
   * Nest tears down in a fixed order: every `onModuleDestroy`, then every
   * `beforeApplicationShutdown`, then every `onApplicationShutdown`. The lanes
   * are stopped in `LaneSupervisor.beforeApplicationShutdown`, and stopping a
   * lane means draining the tick in flight and then RELEASING its lease -- a
   * write. So any connection a lane needs must still be open through the middle
   * phase and may only be closed in the last one.
   *
   * This was `onModuleDestroy`, and it was a real bug with a five-minute blast
   * radius: `pool.end()` ran first, the supervisor's release then failed with
   * "Cannot use a pool after calling end", the lease was never handed back, and
   * the restarted process sat next to work it was not allowed to claim for the
   * whole of `LEASE_SECONDS` -- exactly the symptom §4.2 exists to prevent.
   *
   * Every in-process test of shutdown stayed green throughout, because they
   * drive the hook or `app.close()` and never look at whether the release
   * SUCCEEDED. What caught it was `test/lanes/sigterm-shutdown.e2e-spec.ts`
   * asserting the row afterwards. Do not move this back.
   */
  async onApplicationShutdown(): Promise<void> {
    await this.client.close();
  }
}
