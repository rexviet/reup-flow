import {
  Inject,
  Injectable,
  type BeforeApplicationShutdown,
  type OnApplicationBootstrap,
} from '@nestjs/common';
import { ENV } from '../config/config.module';
import type { Env } from '../config/env';
import { LANE_LOGGER, LANES } from './lanes.tokens';
import type { LaneLogger, LaneRunner } from './lane.runner';

/** How often the staleness watchdog looks. See `checkStaleness`. */
export const WATCHDOG_INTERVAL_MS = 60_000;

/**
 * Owns lane lifecycle: start them at bootstrap, stop them at shutdown, and
 * notice when one has stopped ticking.
 *
 * A class of its own rather than a method on each lane, because of the one
 * thing it can do that a lane cannot: know about the OTHERS. §4.1 asks for an
 * alert when a lane has not ticked for five minutes, and a lane cannot be
 * trusted to raise that about itself -- the lane best placed to notice a dead
 * lane is exactly the one that is dead. Putting the check here means it still
 * fires when the loop that died is the reaper's.
 */
@Injectable()
export class LaneSupervisor implements OnApplicationBootstrap, BeforeApplicationShutdown {
  /**
   * The watchdog's timer, exposed so its `unref()` is assertable rather than
   * merely intended. Null while not running.
   */
  watchdogTimer: NodeJS.Timeout | null = null;

  constructor(
    @Inject(LANES) private readonly lanes: LaneRunner[],
    @Inject(ENV) private readonly env: Env,
    @Inject(LANE_LOGGER) private readonly logger: LaneLogger,
  ) {}

  /**
   * Starts every lane, unless `LANES_ENABLED` says this process is API-only.
   *
   * Each `start()` is attempted independently. One lane that throws on the way
   * up must not take its neighbours with it: a process running five of six
   * download lanes is degraded, whereas a process running none of them is a
   * queue that silently never drains.
   */
  onApplicationBootstrap(): void {
    if (!this.env.LANES_ENABLED) {
      // Said out loud, because a node quietly doing no work is otherwise an
      // unexplainable stuck queue.
      this.logger.log(
        'LANES_ENABLED is false: this process serves HTTP only and will start no lanes',
      );
      return;
    }
    for (const lane of this.lanes) {
      try {
        lane.start();
        this.logger.log(`lane ${lane.name} started (idle delay ${lane.idleDelayMs}ms)`);
      } catch (err: unknown) {
        this.logger.error(`lane ${lane.name} failed to start: ${describe(err)}`);
      }
    }
    this.startWatchdog();
  }

  /**
   * §4.2's shutdown, at the level above one lane: stop the watchdog, then let
   * every lane stop claiming, drain, and release -- all at once.
   *
   * `beforeApplicationShutdown`, NOT `onApplicationShutdown`, and the choice is
   * load-bearing. Nest runs every `onModuleDestroy`, then every
   * `beforeApplicationShutdown`, then every `onApplicationShutdown`. Releasing
   * a lease is a WRITE, so this has to happen while the pg pool is still open
   * -- which is why `DbModule`, `ParserModule` and `RedisModule` close in the
   * LAST phase (see the long note on each). Getting this wrong is not a
   * degraded shutdown, it is no release at all: the lease then holds for its
   * full `LEASE_SECONDS` and the restart idles beside work it may not claim.
   *
   * CONCURRENTLY, not in sequence. Six download lanes draining one after
   * another costs up to 6 x `drainTimeoutMs`, which is well past the ten
   * seconds `docker stop` allows before it SIGKILLs -- and a SIGKILL skips
   * every remaining lane's release, which is the exact outcome the drain was
   * supposed to avoid. Serial shutdown would make the careful part of §4.2
   * self-defeating.
   *
   * A lane that fails to stop is logged, not propagated: its neighbours still
   * have leases to hand back.
   */
  async beforeApplicationShutdown(signal?: string): Promise<void> {
    this.stopWatchdog();
    if (signal) this.logger.log(`received ${signal}: stopping ${this.lanes.length} lane(s)`);
    await Promise.all(
      this.lanes.map(async (lane) => {
        try {
          await lane.stop();
        } catch (err: unknown) {
          this.logger.error(`lane ${lane.name} failed to stop cleanly: ${describe(err)}`);
        }
      }),
    );
  }

  /** Lanes that have not ticked inside their own staleness window. */
  staleLanes(now: number = Date.now()): LaneRunner[] {
    return this.lanes.filter((lane) => lane.isStale(now));
  }

  /**
   * Logs one error line per stale lane -- §4.1's "alert if a lane has not
   * ticked for 5 minutes".
   *
   * An error-level log is the alerting vehicle for now; phase 10 owns turning
   * it into something that pages. It is deliberately phrased so a rule can key
   * on the text, and it names the lane, because "a lane is stuck" without
   * saying which one sends an operator to read four loops.
   */
  checkStaleness(now: number = Date.now()): void {
    for (const lane of this.staleLanes(now)) {
      const last = lane.lastTickAt === null ? 'never' : lane.lastTickAt.toISOString();
      this.logger.error(
        `lane ${lane.name} has not ticked in over ${lane.staleAfterMs}ms (last tick: ${last})`,
      );
    }
  }

  /**
   * A plain interval, not a lane, and that is not an inconsistency.
   *
   * The reasons Approach 1 exists -- pacing inside a single job, concurrency
   * as a count of loops, a directly-callable `runOnce` -- are all about lanes
   * that do WORK. This reads in-memory timestamps and logs. What it does need,
   * and a lane could not provide, is to survive the death of any lane.
   *
   * `unref()` so it can never hold the process open. An un-unref'd 60s
   * interval turns "the work is finished" into "the process hangs for up to a
   * minute", which under jest is a suite that times out instead of failing.
   */
  private startWatchdog(): void {
    if (this.watchdogTimer) return;
    this.watchdogTimer = setInterval(() => this.checkStaleness(), WATCHDOG_INTERVAL_MS);
    this.watchdogTimer.unref();
  }

  private stopWatchdog(): void {
    if (!this.watchdogTimer) return;
    clearInterval(this.watchdogTimer);
    this.watchdogTimer = null;
  }
}

function describe(err: unknown): string {
  if (err instanceof Error) return err.stack ?? err.message;
  return String(err);
}
