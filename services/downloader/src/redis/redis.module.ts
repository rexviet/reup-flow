import {
  Global,
  Inject,
  Module,
  type OnApplicationShutdown,
  type OnModuleInit,
} from "@nestjs/common";
import Redis from "ioredis";
import { ENV } from "../config/config.module";
import type { Env } from "../config/env";

/** Injection token for the `ioredis` client. */
export const REDIS = "REDIS";

/** Bounds how many times ioredis retries the initial connection before giving up. */
const MAX_CONNECT_RETRIES = 3;

/**
 * @Global() so jobs/videos/cookie modules (groups B/C) can `@Inject(REDIS)`
 * without importing this module explicitly everywhere. Also the channel
 * phase 3's cookie endpoints (`/internal/cookie*`) write/read through.
 *
 * `lazyConnect: true` + an explicit `connect()` + `ping()` in `OnModuleInit`
 * is what makes a dead Redis fail BOOT rather than the process silently
 * coming up "healthy" with a client that will only reveal the outage on the
 * first real command. Without `lazyConnect`, ioredis starts connecting the
 * instant the client is constructed -- before Nest has even finished
 * building the dependency graph -- which works but leaves nothing for this
 * class to await, so a boot failure could race past `onModuleInit` instead
 * of being observed by it.
 *
 * `retryStrategy` returning `null` after a small, bounded number of
 * attempts is equally load-bearing: ioredis's default strategy retries
 * forever with a capped backoff, which turns "Redis is down" into a
 * container that starts, reports itself unhealthy on nothing in particular,
 * and hangs -- the exact failure mode this module exists to avoid.
 * `maxRetriesPerRequest: 1` applies the same fail-fast bias to ordinary
 * commands issued after boot succeeded but Redis later drops mid-session.
 */
@Global()
@Module({
  providers: [
    {
      provide: REDIS,
      inject: [ENV],
      useFactory: (env: Env): Redis =>
        new Redis(env.REDIS_URL, {
          lazyConnect: true,
          maxRetriesPerRequest: 1,
          retryStrategy: (attempt: number) =>
            attempt > MAX_CONNECT_RETRIES ? null : Math.min(attempt * 100, 1000),
        }),
    },
  ],
  exports: [REDIS],
})
export class RedisModule implements OnModuleInit, OnApplicationShutdown {
  constructor(
    @Inject(REDIS) private readonly client: Redis,
    @Inject(ENV) private readonly env: Env,
  ) {}

  async onModuleInit(): Promise<void> {
    try {
      await this.client.connect();
      await this.client.ping();
    } catch (err) {
      const cause = err instanceof Error ? err.message : String(err);
      // Nest never calls OnApplicationShutdown for a module whose OnModuleInit
      // threw, so this is the only chance to release the client -- without
      // it, a failed boot probe leaves a half-connected ioredis instance
      // (and its retry timers) dangling for the remainder of the process.
      this.client.disconnect();
      // Same reasoning as DbModule: the DSN is deliberately embedded here.
      // The defence against a secret in a boot error is the redaction
      // layer (installed first thing in main.ts), not scrubbing it at this
      // call site. See db.module.ts's onModuleInit for the fuller version
      // of this comment.
      throw new Error(
        `RedisModule failed its boot probe against ${this.env.REDIS_URL}: ${cause}`,
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
  // No lane touches Redis directly today -- the parser reads the cookie from
  // it, out of process -- so this one is moved for consistency rather than
  // necessity. Leaving it a phase behind the others would be a trap for the
  // first lane that ever does need a cookie value.
  async onApplicationShutdown(): Promise<void> {
    this.client.disconnect();
  }
}
