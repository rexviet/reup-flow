import { Global, Inject, Module, type OnApplicationShutdown } from '@nestjs/common';
import { Client, Connection } from '@temporalio/client';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';

/** Injection token for the `@temporalio/client` `Client`. */
export const TEMPORAL_CLIENT = 'TEMPORAL_CLIENT';
const TEMPORAL_CONNECTION = 'TEMPORAL_CONNECTION';

/**
 * `Connection.lazy` opens no socket until the first call, so the downloader
 * boots whether or not Temporal is up. This is the opposite of `RedisModule` on
 * purpose: Redis is something the downloader cannot work without, Temporal is
 * only something the workspace page reads, and it must not decide whether
 * `/health` passes.
 */
@Global()
@Module({
  providers: [
    {
      provide: TEMPORAL_CONNECTION,
      inject: [ENV],
      useFactory: (env: Env): Connection => Connection.lazy({ address: env.TEMPORAL_ADDRESS }),
    },
    {
      provide: TEMPORAL_CLIENT,
      inject: [ENV, TEMPORAL_CONNECTION],
      useFactory: (env: Env, connection: Connection): Client =>
        new Client({ connection, namespace: env.TEMPORAL_NAMESPACE }),
    },
  ],
  exports: [TEMPORAL_CLIENT],
})
export class TemporalModule implements OnApplicationShutdown {
  constructor(@Inject(TEMPORAL_CONNECTION) private readonly connection: Connection) {}

  async onApplicationShutdown(): Promise<void> {
    await this.connection.close();
  }
}
