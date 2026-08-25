/**
 * Phase 4 §4.2 -- `LaneSupervisor`: the one thing that owns lane lifecycle.
 *
 * §4.1's todo asks for registration on `OnApplicationBootstrap` /
 * `OnApplicationShutdown`. It lives in a class of its own rather than on each
 * lane because of what it can do that a lane cannot: know about the OTHERS.
 * The staleness alert §4.1 asks for ("alert if a lane has not ticked for 5
 * minutes") has to be watched by something that outlives any single lane's
 * loop -- otherwise the lane best placed to notice a dead lane is the dead one.
 *
 * Fake lanes throughout: no database, no container. The supervisor's job is
 * ordering and bookkeeping, and mixing a real Postgres into these assertions
 * would only make them slower and less specific.
 */
import { LaneRunner, type LaneLogger, type TickOutcome } from '../../src/lanes/lane.runner';
import { LaneSupervisor } from '../../src/lanes/lane.supervisor';
import { fixedEnv } from '../support/fixed-env';

class FakeLane extends LaneRunner {
  starts = 0;
  stops = 0;
  idleDelayMs = 60_000;
  /**
   * What this lane answers to `isStale()`.
   *
   * Overridden rather than driven by faking `lastTickAt`: an earlier version of
   * this file redefined the `lastTickAt` GETTER and the assertion failed,
   * because `isStale()` reads the private field the loop writes, not the
   * getter. That was the test reaching for the wrong seam -- and worth
   * recording, because a fake that shadows an accessor the real code does not
   * consult is a fake that can only ever agree with itself. Whether
   * `isStale()` computes the window correctly is lane.runner.spec.ts's
   * question; this file's question is what the supervisor does with the answer.
   */
  stale = true;
  override isStale(): boolean {
    return this.stale;
  }
  constructor(
    readonly name: string,
    logger: LaneLogger,
  ) {
    super(logger);
  }
  async runOnce(): Promise<TickOutcome> {
    return 'idle';
  }
  override start(): void {
    this.starts += 1;
  }
  override stop(): Promise<void> {
    this.stops += 1;
    return Promise.resolve();
  }
}

const logged: string[] = [];
const logger: LaneLogger = {
  error: (m) => logged.push(`ERROR ${m}`),
  warn: (m) => logged.push(`WARN ${m}`),
  log: (m) => logged.push(`LOG ${m}`),
};

beforeEach(() => {
  logged.length = 0;
});

function supervise(lanes: LaneRunner[], lanesEnabled = true): LaneSupervisor {
  return new LaneSupervisor(lanes, fixedEnv({ LANES_ENABLED: lanesEnabled }), logger);
}

describe('bootstrap', () => {
  it('starts every registered lane', async () => {
    const a = new FakeLane('a', logger);
    const b = new FakeLane('b', logger);
    const supervisor = supervise([a, b]);

    supervisor.onApplicationBootstrap();

    expect(a.starts).toBe(1);
    expect(b.starts).toBe(1);
    await supervisor.beforeApplicationShutdown();
  });

  it('starts NOTHING when LANES_ENABLED is false', async () => {
    // The switch that makes plan §0 principle 1 ("Postgres is the only channel
    // between the HTTP layer and a lane") worth something today rather than in
    // principle: with it false this process is an API-only node, and lanes run
    // wherever it is true. Splitting them into separate processes costs no
    // logic change, which is the property principle 1 exists to buy.
    //
    // It is also what keeps every AppModule-based e2e test from starting a
    // reaper against the live compose stack as a side effect of booting.
    const a = new FakeLane('a', logger);
    const supervisor = supervise([a], false);

    supervisor.onApplicationBootstrap();

    expect(a.starts).toBe(0);
    // ...and says so, so a node that is silently doing no work is not a
    // mystery.
    expect(logged.join(' ')).toMatch(/LANES_ENABLED/);
    await supervisor.beforeApplicationShutdown();
  });

  it('starts the remaining lanes even if one refuses to start', async () => {
    const bad = new FakeLane('bad', logger);
    bad.start = () => {
      throw new Error('cannot start');
    };
    const good = new FakeLane('good', logger);
    const supervisor = supervise([bad, good]);

    supervisor.onApplicationBootstrap();

    expect(good.starts).toBe(1);
    expect(logged.join(' ')).toContain('cannot start');
    await supervisor.beforeApplicationShutdown();
  });
});

describe('shutdown', () => {
  it('stops every lane', async () => {
    const a = new FakeLane('a', logger);
    const b = new FakeLane('b', logger);
    const supervisor = supervise([a, b]);
    supervisor.onApplicationBootstrap();

    await supervisor.beforeApplicationShutdown('SIGTERM');

    expect(a.stops).toBe(1);
    expect(b.stops).toBe(1);
  });

  it('stops the other lanes even when one lane fails to stop', async () => {
    // A lane whose release throws must not strand its neighbours mid-download
    // with their leases held -- the whole point of releasing at shutdown is
    // that a restart resumes immediately.
    const bad = new FakeLane('bad', logger);
    bad.stop = () => Promise.reject(new Error('stop failed'));
    const good = new FakeLane('good', logger);
    const supervisor = supervise([bad, good]);
    supervisor.onApplicationBootstrap();

    await expect(supervisor.beforeApplicationShutdown('SIGTERM')).resolves.toBeUndefined();

    expect(good.stops).toBe(1);
    expect(logged.join(' ')).toContain('stop failed');
  });

  it('stops lanes CONCURRENTLY, not one after another', async () => {
    // Six download lanes draining in series would take 6 x drainTimeoutMs in
    // the worst case -- far past the 10s `docker stop` allows before it
    // SIGKILLs, which skips every remaining lane's release.
    const order: string[] = [];
    const slow = (name: string, ms: number) => {
      const lane = new FakeLane(name, logger);
      lane.stop = async () => {
        await new Promise((r) => setTimeout(r, ms));
        order.push(name);
      };
      return lane;
    };
    const supervisor = supervise([slow('slow', 80), slow('fast', 10)]);
    supervisor.onApplicationBootstrap();

    const started = Date.now();
    await supervisor.beforeApplicationShutdown('SIGTERM');

    expect(order).toEqual(['fast', 'slow']);
    expect(Date.now() - started).toBeLessThan(200);
  });

  it('is safe to call without a bootstrap having happened', async () => {
    const supervisor = supervise([new FakeLane('a', logger)]);
    await expect(supervisor.beforeApplicationShutdown()).resolves.toBeUndefined();
  });
});

describe('staleness watchdog (§4.1 metric)', () => {
  it('reports exactly the lanes that call themselves stale, and no others', () => {
    const fresh = new FakeLane('fresh', logger);
    fresh.stale = false;
    const stuck = new FakeLane('stuck', logger);

    const supervisor = supervise([fresh, stuck]);

    expect(supervisor.staleLanes().map((l) => l.name)).toEqual(['stuck']);
  });

  it('logs an error naming each stale lane when checked', () => {
    const supervisor = supervise([new FakeLane('reaper', logger)]);

    supervisor.checkStaleness();

    const line = logged.filter((l) => l.startsWith('ERROR')).join(' ');
    expect(line).toContain('reaper');
    // Named so an alert rule can key on it rather than on prose.
    expect(line).toMatch(/has not ticked/i);
  });

  it('says nothing while every lane is ticking', () => {
    const lane = new FakeLane('reaper', logger);
    lane.stale = false;
    const supervisor = supervise([lane]);

    supervisor.checkStaleness();

    expect(logged.filter((l) => l.startsWith('ERROR'))).toEqual([]);
  });

  it('runs the check on a timer that can never hold the process open', async () => {
    // A plain interval, not a lane, and deliberately so: it reads in-memory
    // timestamps and logs, so none of the reasons Approach 1 exists (pacing
    // inside a job, structural concurrency) apply to it. What does apply is
    // that a lane cannot watch itself -- so this is the one thing that
    // notices when the REAPER is the loop that died.
    //
    // `unref()` is load-bearing: an interval that keeps the event loop alive
    // turns "the process finished its work" into "the process hangs", which is
    // how a test suite starts timing out instead of failing.
    const supervisor = supervise([new FakeLane('a', logger)]);
    supervisor.onApplicationBootstrap();

    expect(supervisor.watchdogTimer).not.toBeNull();
    expect(supervisor.watchdogTimer!.hasRef()).toBe(false);

    await supervisor.beforeApplicationShutdown();
    expect(supervisor.watchdogTimer).toBeNull();
  });
});
