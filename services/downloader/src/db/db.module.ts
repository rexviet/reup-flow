import {
  Global,
  Inject,
  Logger,
  Module,
  type OnApplicationShutdown,
  type OnModuleInit,
} from '@nestjs/common';
import { Pool } from 'pg';
import { drizzle, type NodePgDatabase } from 'drizzle-orm/node-postgres';
import * as schema from './schema';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';

/** Injection token for the underlying `pg.Pool`. */
export const PG_POOL = 'PG_POOL';
/** Injection token for the drizzle handle typed against this service's schema. */
export const DB = 'DB';

/**
 * A refused connection through `pg.Pool` rejects with a Node `AggregateError`
 * whose OWN `.message` and `.stack` are empty of any useful text -- Node
 * attempts every resolved address for `localhost` (IPv6 then IPv4) and
 * bundles each attempt's real error (each with its own "connect ECONNREFUSED
 * ..." message) into `.errors`, never surfacing them on the top-level error
 * at all. Naively using `err.message` here would silently produce an error
 * with no cause text and no "ECONNREFUSED" -- which is exactly what
 * happened the first time this was written, and is why this helper exists
 * instead of the obvious one-liner.
 */
function describeConnectionError(err: unknown): string {
  if (err instanceof Error) {
    const nested = (err as { errors?: unknown[] }).errors;
    if (Array.isArray(nested) && nested.length > 0) {
      return nested.map((e) => (e instanceof Error ? e.message : String(e))).join('; ');
    }
    return err.message || err.stack || err.name;
  }
  return String(err);
}

/**
 * The pool, with a listener for errors on IDLE clients. When Postgres ends a
 * connection the pool is holding (a restart sends `57P01 terminating connection
 * due to administrator command`), pg emits `error` on the pool. An EventEmitter
 * with no `error` listener throws, which took the whole process down and left
 * the dashboard answering 502 until someone started the container again. pg has
 * already dropped the dead client when it emits, so logging is all that is
 * needed: the next query opens a fresh connection.
 */
export function createPool(
  connectionString: string,
  logger: Pick<Logger, 'warn'> = new Logger('DbModule'),
): Pool {
  const pool = new Pool({ connectionString });
  pool.on('error', (err) => {
    logger.warn(`idle Postgres connection dropped: ${describeConnectionError(err)}`);
  });
  return pool;
}

/**
 * @Global() so jobs/videos/cookie repositories (groups B/C) can `@Inject
 * (DB)` without every feature module importing this one explicitly.
 *
 * `OnModuleInit` runs `select 1` so a bad DSN crashes at BOOT, not on the
 * first job -- fail-fast is already this service's contract (see the
 * env-validation crash path in main.ts). Without this probe, `new Pool(...)`
 * succeeds unconditionally (pg connects lazily) and the very first claim
 * query in a lane would be the one to discover the DSN is wrong, long after
 * `docker compose up` reported healthy.
 */
@Global()
@Module({
  providers: [
    {
      provide: PG_POOL,
      inject: [ENV],
      useFactory: (env: Env): Pool => createPool(env.DATABASE_URL),
    },
    {
      provide: DB,
      inject: [PG_POOL],
      useFactory: (pool: Pool): NodePgDatabase<typeof schema> => drizzle(pool, { schema }),
    },
  ],
  exports: [PG_POOL, DB],
})
export class DbModule implements OnModuleInit, OnApplicationShutdown {
  constructor(
    @Inject(PG_POOL) private readonly pool: Pool,
    @Inject(ENV) private readonly env: Env,
  ) {}

  async onModuleInit(): Promise<void> {
    try {
      await this.pool.query('select 1');
    } catch (err) {
      const cause = describeConnectionError(err);
      // Deliberately including the full DSN (which embeds the password) in
      // this thrown message. That is NOT an oversight: this is exactly what
      // real infra clients (pg, ioredis, ...) already do in their own
      // connection-error messages, and plan §0.3 is explicit that the
      // defence against a secret in an error message is the redaction
      // LAYER (src/redaction/redactor.ts + the stdio patch installed first
      // thing in main.ts), never call-site discipline. Scrubbing the DSN
      // here would also remove the only realistic, unmocked case that
      // proves the redaction layer actually catches a real infra error --
      // do not "fix" this into a redacted message.
      throw new Error(
        `DbModule failed its boot probe against ${this.env.DATABASE_URL}: ${cause}`,
      );
    }
  }

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
    await this.pool.end();
  }
}
