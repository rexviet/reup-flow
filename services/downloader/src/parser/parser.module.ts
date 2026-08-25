import { Global, Inject, Module, type OnModuleDestroy } from '@nestjs/common';
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
 * `OnModuleDestroy` calls `client.close()`. The client owns a private undici
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
export class ParserModule implements OnModuleDestroy {
  constructor(@Inject(PARSER_CLIENT) private readonly client: ParserClient) {}

  async onModuleDestroy(): Promise<void> {
    await this.client.close();
  }
}
