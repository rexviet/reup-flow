import { Module } from '@nestjs/common';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { DB } from '../db/db.module';
import type { Executor } from '../db/executor';
import { RedactingLogger } from '../logging/redact.logger';
import { PARSER_CLIENT } from '../parser/parser.module';
import { STORAGE } from '../storage/storage.module';
import type { Storage } from '../storage/storage.interface';
import { EnumerateLane, type PostPageSource } from './enumerate.lane';
import { LaneSupervisor } from './lane.supervisor';
import type { LaneLogger, LaneRunner } from './lane.runner';
import { LANE_LOGGER, LANES } from './lanes.tokens';
import { ReaperLane } from './reaper.lane';

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
        parser: PostPageSource,
        env: Env,
      ): EnumerateLane =>
        new EnumerateLane(logger, db, parser, {
          leaseSeconds: env.LEASE_SECONDS,
          urlTtlSeconds: env.URL_TTL_SECONDS,
          sleepMsMin: env.ENUMERATE_SLEEP_MS_MIN,
          sleepMsMax: env.ENUMERATE_SLEEP_MS_MAX,
        }),
    },
    {
      provide: LANES,
      inject: [ReaperLane, EnumerateLane],
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
      useFactory: (reaper: ReaperLane, enumerate: EnumerateLane): LaneRunner[] => [
        reaper,
        enumerate,
      ],
    },
    LaneSupervisor,
  ],
  exports: [LANES],
})
export class LanesModule {}
