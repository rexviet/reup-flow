/**
 * F2 -- ENUMERATE CONCURRENCY IS 1 FOR THE WHOLE SYSTEM, NOT 1 PER JOB.
 *
 * Plan §5's mutation row: "Enumerate global 1 luồng -- đổi thành per-job -- test
 * tần suất 2 job ĐỎ". Plan §9's DoD line: "Enumerate 2 job song song → tổng tần
 * suất request vẫn = 1 luồng".
 *
 * Douyin rate-limits by IP, not by job. Three operators starting three jobs is
 * three times the request rate from one address, and that is a ban. So this
 * asks the only question that matters: with two jobs queued, how many requests
 * does the SYSTEM have in flight at once?
 *
 * WHY THIS DRIVES THE PRODUCTION `LANES` ARRAY. F2 is not a property of
 * `EnumerateLane` -- nothing inside that class could enforce it. It is a
 * property of how many instances `lanes.module.ts` registers (solution design
 * §4: concurrency IS the number of loops). A test that constructed its own lane
 * would prove that one lane behaves like one lane, which is a tautology, and
 * would stay green through the exact edit this exists to catch. So the lanes
 * under test are resolved out of the real container.
 *
 * WHY THERE IS A POSITIVE CONTROL AT THE BOTTOM. "Max in-flight was 1" is only
 * evidence if this harness can SEE a 2. An overlap counter that never fires --
 * because the fake resolves too fast, or because the counter is wired wrong --
 * reports perfect compliance from a completely broken system. The control runs
 * two lanes deliberately and requires the instrument to report 2. Without it
 * the assertion above is unfalsifiable, which is the shape this repo has now
 * shipped four times.
 */
import { Test } from '@nestjs/testing';
import type { Pool } from 'pg';
import { AppModule } from '../../src/app.module';
import { jobs, profiles } from '../../src/db/schema';
import {
  EnumerateLane,
  type PostPageSource,
} from '../../src/lanes/enumerate.lane';
import type { LaneRunner } from '../../src/lanes/lane.runner';
import { LANES } from '../../src/lanes/lanes.tokens';
import { PARSER_CLIENT } from '../../src/parser/parser.module';
import type { UserPostPage, UserProfile } from '../../src/parser/parser.client';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
} from '../support/db';

jest.setTimeout(120_000);

const SCHEMA = testSchemaName('enumerate_f2');
const PROFILE_A = 'MS4wLjABAAAA_f2_a';
const PROFILE_B = 'MS4wLjABAAAA_f2_b';

/**
 * The pacing, chosen so that OVERLAP IS FORCED rather than merely likely.
 *
 * Each lane holds a request open for `HOLD_MS` and then waits at most
 * `SLEEP_MAX_MS`, so it is in flight for HOLD/(HOLD+SLEEP_MAX) = 80/110 = 73%
 * of its cycle. Two independent lanes at 73% duty cannot both stay out of each
 * other's way: by the pigeonhole principle they must be in flight together.
 * That is what makes the mutation's red deterministic instead of a coin flip.
 */
const HOLD_MS = 80;
const SLEEP_MIN_MS = 20;
const SLEEP_MAX_MS = 30;
const PAGES_PER_JOB = 4;

let pool: Pool;
let db: ReturnType<typeof testDb>;

/**
 * A page source that answers slowly and counts how many answers are in flight.
 *
 * `maxInFlight` is the whole measurement. Recording the request TIMES as well,
 * because "no two at once" and "spaced out" are different claims and only the
 * second one is about the rate.
 */
class ConcurrencyProbe implements PostPageSource {
  inFlight = 0;
  maxInFlight = 0;
  readonly startedAt: number[] = [];
  private readonly served = new Map<string, number>();

  /**
   * `ParserModule.onApplicationShutdown` calls `close()` on whatever is bound
   * to PARSER_CLIENT. Leaving it off is not a missing convenience: the throw
   * lands inside Nest's shutdown, so the pg pool and the Redis client are never
   * closed and the suite HANGS with no output at all rather than failing. (Cost
   * one debugging round on first write.)
   */
  async close(): Promise<void> {}

  /**
   * The F4 probe seam. THROWS, because this suite measures the request rate of
   * the HAPPY path: a probe answering here would be a request this instrument
   * counts as enumerate traffic, quietly loosening the very number the DoD line
   * pins ("tổng tần suất request vẫn = 1 luồng").
   */
  async fetchUserProfile(): Promise<UserProfile> {
    throw new Error('the F4 probe must not run while measuring the happy-path request rate');
  }

  /**
   * Full reset -- `inFlight` included, and that one is not housekeeping.
   *
   * If a request is still in flight when this runs (a drain that timed out), a
   * leftover `inFlight` of 1 makes the positive control's `maxInFlight === 2`
   * satisfiable with no overlap at all. The one assertion whose entire job is to
   * prove this instrument can fail would then be the assertion that cannot.
   */
  reset(): void {
    this.inFlight = 0;
    this.maxInFlight = 0;
    this.startedAt.length = 0;
    this.served.clear();
  }

  async fetchUserPostPage(
    secUserId: string,
    _maxCursor: number,
    _count: number,
  ): Promise<UserPostPage> {
    this.inFlight += 1;
    this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);
    this.startedAt.push(Date.now());
    try {
      await new Promise((r) => setTimeout(r, HOLD_MS));
      const page = (this.served.get(secUserId) ?? 0) + 1;
      this.served.set(secUserId, page);
      return {
        // Distinct `aweme_id`s per profile so the two jobs never collide on the
        // primary key -- a conflict would silently shorten one of them.
        awemeList: [
          {
            aweme_id: `${secUserId}-p${page}`,
            desc: null,
            create_time: 1_700_000_000 + page,
            video: { duration: 1_000, play_addr: { url_list: ['https://cdn.example/x.mp4'] } },
          },
        ],
        hasMore: page < PAGES_PER_JOB,
        // DESCENDING, as Douyin's really is.
        maxCursor: 1_800_000_000_000 - page * 1_000_000,
        statusCode: 0,
      };
    } finally {
      this.inFlight -= 1;
    }
  }
}

/** DSN pinned to this suite's schema, so the real AppModule uses it. */
function schemaScopedDsn(schema: string): string {
  const base = process.env.TEST_DATABASE_URL ?? 'postgres://reup:reup@localhost:5432/reup';
  const url = new URL(base);
  url.searchParams.set('options', `-c search_path=${schema}`);
  return url.toString();
}

const originalEnv = { ...process.env };
let probe: ConcurrencyProbe;
let lanes: LaneRunner[];
let close: (() => Promise<void>) | undefined;

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);

  Object.assign(process.env, {
    API_KEY: 'enumerate-f2-test-key-not-real',
    DATABASE_URL: schemaScopedDsn(SCHEMA),
    REDIS_URL: 'redis://localhost:6379/9',
    PARSER_BASE_URL: 'http://parser:80',
    PROBE_SEC_USER_ID: 'probe-sec-user-id',
    // Compressed pacing, read through the real env schema and the real factory
    // -- so this also proves the lane is wired to these vars at all.
    ENUMERATE_SLEEP_MS_MIN: String(SLEEP_MIN_MS),
    ENUMERATE_SLEEP_MS_MAX: String(SLEEP_MAX_MS),
    // OFF: the lanes are started by hand below, one kind at a time, so the
    // reaper never sweeps while this is measuring.
    LANES_ENABLED: 'false',
  });

  probe = new ConcurrencyProbe();
  const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
    .overrideProvider(PARSER_CLIENT)
    .useValue(probe)
    .compile();
  const app = moduleRef.createNestApplication();
  await app.init();
  lanes = moduleRef.get<LaneRunner[]>(LANES);
  close = () => app.close();
});

afterAll(async () => {
  await close?.();
  await pool.end();
  await dropSchema(SCHEMA);
  process.env = { ...originalEnv };
});

async function seedTwoJobs(): Promise<void> {
  await db
    .insert(profiles)
    .values([{ secUserId: PROFILE_A }, { secUserId: PROFILE_B }])
    .onConflictDoNothing();
  await db.insert(jobs).values([{ secUserId: PROFILE_A }, { secUserId: PROFILE_B }]);
}

async function countDownloading(): Promise<number> {
  const { rows } = await pool.query("SELECT count(*)::int AS n FROM jobs WHERE status = 'downloading'");
  return (rows[0] as { n: number }).n;
}

/** Runs `toRun` until both jobs are done with enumeration, or the deadline passes. */
async function drain(toRun: LaneRunner[], budgetMs = 20_000): Promise<void> {
  expect(toRun.length).toBeGreaterThan(0);
  for (const lane of toRun) lane.start();
  const deadline = Date.now() + budgetMs;
  while ((await countDownloading()) < 2 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 25));
  }
  await Promise.all(toRun.map((lane) => lane.stop()));
  // Said out loud rather than left to the assertions: a lane that crashed on
  // its first tick produces a perfect `maxInFlight` of 0, and "nothing ran"
  // must never be able to read as "the limit held".
  for (const lane of toRun) expect(lane.crashedWith).toBeNull();
}

describe('two jobs queued at once', () => {
  it('never has more than ONE parser request in flight, system-wide', async () => {
    const enumerateLanes = lanes.filter((lane) => lane instanceof EnumerateLane);
    await seedTwoJobs();

    await drain(enumerateLanes);

    // Both jobs really did get enumerated -- otherwise "max in flight 1" is
    // just a description of a lane that never ran.
    expect(await countDownloading()).toBe(2);
    expect(probe.startedAt).toHaveLength(2 * PAGES_PER_JOB);
    // THE assertion. One loop means one request at a time, whatever the queue
    // depth. Two registered lanes make this 2.
    expect(probe.maxInFlight).toBe(1);
  });

  it('spaces requests WITHIN a job by at least the configured floor', async () => {
    // "One at a time" is not the whole of F2 -- a single lane firing serial
    // requests with no delay is still one thread and still a ban. The gaps
    // between pages of one job are where the actual rate lives.
    //
    // Gaps BETWEEN jobs are deliberately excluded: a lane that finishes job A
    // comes straight back for job B, and the back-to-back pair that produces is
    // correct. It is two requests from one thread, which is what F2 promises.
    const perJob = 2 * PAGES_PER_JOB;
    expect(probe.startedAt).toHaveLength(perJob);
    const gaps = probe.startedAt.slice(1).map((at, i) => at - probe.startedAt[i]!);
    // Each job's own pages must be at least HOLD + SLEEP_MIN apart. Exactly one
    // gap in the sequence is the job handover, so allow one to be shorter.
    const tooClose = gaps.filter((gap) => gap < HOLD_MS + SLEEP_MIN_MS);
    expect(tooClose.length).toBeLessThanOrEqual(1);
  });
});

describe('positive control: the instrument can see overlap', () => {
  it('reports 2 in flight when two enumerate lanes really are running', async () => {
    // Without this, `maxInFlight === 1` above is unfalsifiable: a counter that
    // is wired wrong, or a fake that resolves before its caller yields, reports
    // flawless compliance from a system with six lanes in it.
    //
    // Two lanes constructed HERE, not registered in the module -- this control
    // is about the measuring instrument, not about production wiring.
    const template = lanes.find((lane) => lane instanceof EnumerateLane) as EnumerateLane;
    const twin = () =>
      new EnumerateLane(
        { error: () => {}, warn: () => {}, log: () => {} },
        db,
        probe,
        {
          leaseSeconds: 300,
          urlTtlSeconds: 7200,
          sleepMsMin: SLEEP_MIN_MS,
          sleepMsMax: SLEEP_MAX_MS,
          probeSecUserId: 'probe-sec-user-id',
        },
      );
    expect(template).toBeInstanceOf(EnumerateLane);

    probe.reset();
    await pool.query('TRUNCATE videos, jobs, profiles RESTART IDENTITY CASCADE');
    await seedTwoJobs();

    await drain([twin(), twin()]);

    expect(await countDownloading()).toBe(2);
    expect(probe.maxInFlight).toBe(2);
  });
});
