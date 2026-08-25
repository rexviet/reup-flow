import { Module } from '@nestjs/common';
import { CDN_CLIENT } from '../cdn/cdn.module';
import type { CdnClient } from '../cdn/cdn.client';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { DB } from '../db/db.module';
import type { Executor } from '../db/executor';
import { RedactingLogger } from '../logging/redact.logger';
import { PARSER_CLIENT } from '../parser/parser.module';
import { STORAGE } from '../storage/storage.module';
import type { Storage } from '../storage/storage.interface';
import { DownloadLane, type VideoUrlSource } from './download.lane';
import { EnumerateLane, type EnumerateParser } from './enumerate.lane';
import { LaneSupervisor } from './lane.supervisor';
import type { LaneLogger, LaneRunner } from './lane.runner';
import { DOWNLOAD_LANES, LANE_LOGGER, LANES } from './lanes.tokens';
import { ReaperLane } from './reaper.lane';
import { ProbeHealthCheck } from '../parser/probe-health';
import type { ProbeSource } from '../parser/f4-probe';

/**
 * The lane layer.
 *
 * The `LANES` provider is the ONLY place that decides how many loops of each
 * kind exist, and that is the point rather than a convenience. Approach 1's
 * concurrency control is structural (solution design §4): "at most N
 * concurrent downloads" is true because there are N instances in this array,
 * not because a counter says so. F2's "exactly one enumerate lane, globally"
 * will be one entry here for the same reason. A semaphore that miscounts is
 * a silent bug; an array with the wrong number of elements is a diff.
 *
 * NOTHING under `src/jobs`, `src/videos`, or `src/cookie` may import this
 * module or anything else in this directory -- plan §0 principle 1, enforced
 * by `.eslintrc.json`'s `no-restricted-imports` at lint time and by
 * `test/architecture/http-lane-boundary.spec.ts` at test time (which catches
 * the dynamic-import and computed-`require` bypasses lint cannot see).
 * `AppModule` imports both sides; that is the only place they meet, and they
 * still only communicate through Postgres.
 */
@Module({
  providers: [
    {
      // One `RedactingLogger`, shared. Lanes handle Douyin responses and
      // parser errors, so anything they log goes through the redaction layer
      // plan §0.3 requires -- not through `console.log`, which the stdio patch
      // would scrub but which would bypass Nest's context and levels.
      provide: LANE_LOGGER,
      useFactory: (): LaneLogger => new RedactingLogger('Lane'),
    },
    {
      provide: ReaperLane,
      inject: [LANE_LOGGER, DB, STORAGE, ENV],
      useFactory: (logger: LaneLogger, db: Executor, storage: Storage, env: Env): ReaperLane =>
        new ReaperLane(logger, db, storage, {
          leaseSeconds: env.LEASE_SECONDS,
          maxAttempts: env.MAX_ATTEMPTS,
        }),
    },
    {
      provide: EnumerateLane,
      inject: [LANE_LOGGER, DB, PARSER_CLIENT, ENV],
      useFactory: (
        logger: LaneLogger,
        db: Executor,
        parser: EnumerateParser,
        env: Env,
      ): EnumerateLane =>
        new EnumerateLane(logger, db, parser, {
          leaseSeconds: env.LEASE_SECONDS,
          urlTtlSeconds: env.URL_TTL_SECONDS,
          sleepMsMin: env.ENUMERATE_SLEEP_MS_MIN,
          sleepMsMax: env.ENUMERATE_SLEEP_MS_MAX,
          // F4. Wired from env rather than hardcoded because the one profile
          // that is "known public" is an operational fact, not a constant --
          // and a probe target that has gone private inverts F4 silently, which
          // is what `ProbeHealthCheck` watches for at boot.
          probeSecUserId: env.PROBE_SEC_USER_ID,
        }),
    },
    {
      /**
       * `DOWNLOAD_CONCURRENCY` download lanes, and the ARRAY is the limit.
       *
       * §6.1: "Concurrency = số loop, không phải semaphore." Six independent
       * loops cannot exceed six concurrent downloads, by construction -- there
       * is no counter to miscount and no place to acquire twice. That is the
       * same argument as F2 one line below, reaching the opposite number for a
       * reason that is measured rather than guessed: `*.douyinvod.com` serves
       * bytes and is not behind the risk control that rate-limits the web API by
       * IP (IDEA.md §3.1 vs §3.2), so 4-8 streams are safe where 2 enumerate
       * loops are a ban.
       *
       * The index is 1-based and only names the lane. Six loops all called
       * `download` would make §4.1's staleness alert say "a lane is stuck"
       * without saying which -- and one wedged socket out of six is exactly what
       * that alert is for.
       */
      provide: DOWNLOAD_LANES,
      inject: [LANE_LOGGER, DB, PARSER_CLIENT, CDN_CLIENT, STORAGE, ENV],
      useFactory: (
        logger: LaneLogger,
        db: Executor,
        parser: VideoUrlSource,
        cdn: CdnClient,
        storage: Storage,
        env: Env,
      ): DownloadLane[] =>
        Array.from(
          { length: env.DOWNLOAD_CONCURRENCY },
          (_unused, i) =>
            new DownloadLane(logger, db, parser, cdn, storage, {
              index: i + 1,
              leaseSeconds: env.LEASE_SECONDS,
              maxAttempts: env.MAX_ATTEMPTS,
              urlTtlSeconds: env.URL_TTL_SECONDS,
            }),
        ),
    },
    {
      provide: LANES,
      inject: [ReaperLane, EnumerateLane, DOWNLOAD_LANES],
      // Exactly one reaper: a second would double every sweep's work for no
      // gain, since the sweeps are idempotent and the only effect is
      // contention.
      //
      // EXACTLY ONE ENUMERATE LANE, AND THIS LINE IS WHERE F2 LIVES.
      //
      // Douyin rate-limits by IP, not by job. Three operators starting three
      // jobs is three times the request rate from one address, which is a ban
      // -- so the second job has to wait, and what makes it wait is that there
      // is one loop to wait for. Not a semaphore (which can miscount), not a
      // config value (which can be edited without anybody reading the reason):
      // one element in this array. That is Approach 1's whole argument
      // (solution design §4), and it is why "add a second one for throughput"
      // is a diff rather than a runtime surprise.
      //
      // The throughput ceiling is the IP, not this lane. Adding a second entry
      // here does not double the rate at which videos arrive; it doubles the
      // rate at which the cookie gets banned.
      // `test/lanes/enumerate-global-concurrency.spec.ts` measures the request
      // rate through this exact array so the diff cannot pass silently.
      useFactory: (
        reaper: ReaperLane,
        enumerate: EnumerateLane,
        downloads: DownloadLane[],
      ): LaneRunner[] => [reaper, enumerate, ...downloads],
    },
    {
      /**
       * F4's boot-time sanity check on the probe target (phase-07 Risks row 1).
       *
       * Registered HERE, in the lane module, because that is where its enabling
       * condition lives: only a process that runs lanes ever diagnoses anything,
       * so only such a process should spend the request. `LANES_ENABLED` gates
       * it for exactly the same reason it gates the lanes themselves.
       *
       * A factory rather than a class provider, so `enabled` and
       * `probeSecUserId` are read from env in one visible place instead of
       * through decorators that would then have to be kept in step with it.
       */
      provide: ProbeHealthCheck,
      inject: [LANE_LOGGER, PARSER_CLIENT, ENV],
      useFactory: (logger: LaneLogger, parser: ProbeSource, env: Env): ProbeHealthCheck =>
        new ProbeHealthCheck(logger, parser, {
          probeSecUserId: env.PROBE_SEC_USER_ID,
          enabled: env.LANES_ENABLED,
        }),
    },
    LaneSupervisor,
  ],
  exports: [LANES, ProbeHealthCheck],
})
export class LanesModule {}
