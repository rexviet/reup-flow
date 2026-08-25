import { Module } from '@nestjs/common';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { DB } from '../db/db.module';
import type { Executor } from '../db/executor';
import { RedactingLogger } from '../logging/redact.logger';
import { STORAGE } from '../storage/storage.module';
import type { Storage } from '../storage/storage.interface';
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
      provide: LANES,
      inject: [ReaperLane],
      // Exactly one reaper. A second would double every sweep's work for no
      // gain: the sweeps are idempotent, so the only effect is contention.
      useFactory: (reaper: ReaperLane): LaneRunner[] => [reaper],
    },
    LaneSupervisor,
  ],
  exports: [LANES],
})
export class LanesModule {}
