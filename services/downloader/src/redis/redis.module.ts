import { Global, Inject, Module, type OnModuleDestroy, type OnModuleInit } from "@nestjs/common";
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
export class RedisModule implements OnModuleInit, OnModuleDestroy {
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
      // Nest never calls OnModuleDestroy for a module whose OnModuleInit
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

  async onModuleDestroy(): Promise<void> {
    this.client.disconnect();
  }
}
