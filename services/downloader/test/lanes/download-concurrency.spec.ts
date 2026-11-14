/**
 * phase-07 §7.3, SECOND PHASE -- "Download: N video đồng thời -> đo có đúng
 * `DOWNLOAD_CONCURRENCY` request bay cùng lúc, không hơn."
 *
 * The two phases are rate-limited COMPLETELY DIFFERENTLY (IDEA.md §3.1 vs
 * §3.2), and that is the point of measuring them separately.
 * `*.douyinvod.com` serves bytes and is not behind the risk control that
 * rate-limits the web API by IP, so download concurrency is 6 where enumerate's
 * is 1. Applying one limit to both would be ten times slower than necessary;
 * applying the download limit to enumerate would be a ban. So there are two
 * suites, and this one is the download half. Enumerate's is
 * `enumerate-global-concurrency.spec.ts`.
 *
 * WHAT THIS MEASURES, AND WHY THE OBVIOUS VERSION IS WORTHLESS.
 *
 * `Promise.all` is PARALLEL DISPATCH, NOT OVERLAPPING EXECUTION. Counting how
 * many times the CDN client was CALLED answers a different question from how
 * many requests were in flight AT ONCE, and this repo has already shipped tests
 * named for concurrency that never overlapped anything (plan §5, the SKIP LOCKED
 * row). So the instrument here counts requests that are simultaneously OPEN on a
 * real HTTP server, and every request is held until either the expected number
 * are open together or a deadline passes -- so a run that never overlapped fails
 * with a number rather than passing quietly.
 *
 * THE LANES COME OUT OF THE REAL CONTAINER. §6.1: "concurrency = số loop, không
 * phải semaphore". The limit is the LENGTH OF THE `DOWNLOAD_LANES` ARRAY, which
 * is a property of `lanes.module.ts` and env, not of `DownloadLane`. A suite that
 * constructed its own lanes would prove that N lanes behave like N lanes -- a
 * tautology -- and would stay green through the edit that hard-codes the length.
 * `DOWNLOAD_CONCURRENCY` is set to a value that is NOT the default 6, so a run
 * cannot confuse "reads the env var" with "happens to be 6".
 *
 * AND THERE IS A POSITIVE CONTROL. "Never more than 3 in flight" is only
 * evidence if this harness can SEE a 4. An overlap counter wired wrong, or a
 * server fast enough that requests never coexist, reports perfect compliance
 * from a completely unbounded system. The control runs MORE lanes deliberately
 * and requires the instrument to report the higher number.
 */
import { Test } from '@nestjs/testing';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Pool } from 'pg';
import { AppModule } from '../../src/app.module';
import { CDN_CLIENT } from '../../src/cdn/cdn.module';
import type { CdnClient } from '../../src/cdn/cdn.client';
import { DownloadLane } from '../../src/lanes/download.lane';
import type { LaneLogger, LaneRunner } from '../../src/lanes/lane.runner';
import { DOWNLOAD_LANES, LANES } from '../../src/lanes/lanes.tokens';
import { PARSER_CLIENT } from '../../src/parser/parser.module';
import { STORAGE } from '../../src/storage/storage.module';
import type { Storage } from '../../src/storage/storage.interface';
import {
  TEST_DATABASE_URL,
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
} from '../support/db';
import { channelCollection } from '../support/collections';
import { FakeCdn } from '../support/fake-cdn';

jest.setTimeout(180_000);

const SCHEMA = testSchemaName('download_concurrency');
const PROFILE = 'MS4wLjABAAAA_dl_conc';
/**
 * Deliberately NOT the production default of 6. A run at the default cannot
 * distinguish "the array is built from DOWNLOAD_CONCURRENCY" from "the array is
 * hard-coded to 6", which is exactly the mutation this suite has to catch.
 */
const CONCURRENCY = 3;
/** Comfortably more than the concurrency, so the loops keep finding work. */
const VIDEO_COUNT = 18;
/** Enough lanes for the positive control to exceed the production number. */
const CONTROL_LANES = CONCURRENCY + 2;

let pool: Pool;
let db: ReturnType<typeof testDb>;
let root: string;
let productionLanes: DownloadLane[];
let close: (() => Promise<void>) | undefined;
let cdnClient: CdnClient;
let storage: Storage;

const cdn = new FakeCdn();
const originalEnv = { ...process.env };
const quietLogger: LaneLogger = { error: () => {}, warn: () => {}, log: () => {} };

/**
 * Counts requests that are OPEN AT THE SAME TIME on a real server, and forces
 * them to coexist.
 *
 * `releaseAt` is what makes the measurement deterministic rather than lucky:
 * every request parks until that many are simultaneously open, and only then do
 * they all finish. A run in which the lanes never actually overlap therefore
 * cannot pass by being fast -- it waits out `HOLD_CEILING_MS` and reports the
 * real (too low) maximum.
 */
class OverlapMeter {
  inFlight = 0;
  maxInFlight = 0;
  total = 0;
  releaseAt = 1;
  /**
   * Never let a shortfall hang the suite; report the number instead.
   *
   * Small on purpose. It is only ever paid when the barrier is NOT met -- i.e.
   * when the system under test is more serial than it claims -- and it is paid
   * once per unmet request, so a large value turns a clear failure into a
   * timeout. `18 x 400ms` is a fast red; `18 x 1500ms` plus a leaked pg pool was
   * a 300-second hang the harness could only report as "caught by hanging".
   */
  static readonly HOLD_CEILING_MS = 400;

  private waiters: (() => void)[] = [];

  reset(releaseAt: number): void {
    this.inFlight = 0;
    this.maxInFlight = 0;
    this.total = 0;
    this.releaseAt = releaseAt;
    this.waiters = [];
  }

  private releaseAll(): void {
    const pending = this.waiters;
    this.waiters = [];
    for (const done of pending) done();
  }

  responder = (req: IncomingMessage, res: ServerResponse): void => {
    const payload = payloadFor(req.url ?? '');
    this.inFlight += 1;
    this.total += 1;
    this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);

    const finish = (): void => {
      this.inFlight -= 1;
      res.writeHead(200, { 'content-length': String(payload.length) });
      res.end(payload);
    };

    if (this.inFlight >= this.releaseAt) {
      // The barrier is satisfied: let this one and everyone waiting go.
      const waiting = this.waiters;
      this.waiters = [];
      finish();
      for (const done of waiting) done();
      return;
    }
    let settled = false;
    const once = (): void => {
      if (settled) return;
      settled = true;
      finish();
    };
    this.waiters.push(once);
    // The escape hatch. Without it a system that serialises downloads would
    // deadlock here, and a hang is a much worse failure report than a number.
    setTimeout(() => {
      this.releaseAll();
      once();
    }, OverlapMeter.HOLD_CEILING_MS).unref();
  };
}

const meter = new OverlapMeter();

/** A different length per video, so a mixed-up file would show. */
function payloadFor(url: string): Buffer {
  const n = Number(/(\d+)/.exec(url)?.[1] ?? 0);
  return Buffer.alloc(1_024 + n, (n % 251) + 1);
}

function awemeIdFor(i: number): string {
  return `dlc-${String(i).padStart(3, '0')}`;
}

function schemaScopedDsn(): string {
  const url = new URL(TEST_DATABASE_URL);
  url.searchParams.set('options', `-c search_path=${SCHEMA}`);
  return url.toString();
}

async function seedProfileAndVideos(): Promise<void> {
  await pool.query('INSERT INTO profiles (sec_user_id) VALUES ($1)', [PROFILE]);
  await pool.query(
    "INSERT INTO jobs (sec_user_id, status, total_videos, enumerated_count) VALUES ($1, 'downloading', $2, $2)",
    [PROFILE, VIDEO_COUNT],
  );
  const collectionId = await channelCollection(pool, PROFILE);
  for (let i = 0; i < VIDEO_COUNT; i += 1) {
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, play_addr, url_expires_at, collection_id)
       VALUES ($1, $2, now() - make_interval(secs => $3), $4, now() + interval '1 hour', $5)`,
      [awemeIdFor(i), PROFILE, VIDEO_COUNT - i, cdn.url(`/${awemeIdFor(i)}.mp4`), collectionId],
    );
  }
}

async function truncate(): Promise<void> {
  await pool.query('TRUNCATE videos, jobs, profiles RESTART IDENTITY CASCADE');
  for (const entry of await readdir(root)) {
    await rm(join(root, entry), { recursive: true, force: true });
  }
}

async function outstanding(): Promise<number> {
  const { rows } = await pool.query(
    "SELECT count(*)::int AS n FROM videos WHERE status IN ('pending','downloading')",
  );
  return (rows[0] as { n: number }).n;
}

/** Runs `lanes` until every video has settled, or the budget runs out. */
async function drain(lanes: LaneRunner[], budgetMs = 60_000): Promise<void> {
  expect(lanes.length).toBeGreaterThan(0);
  for (const lane of lanes) lane.start();
  const deadline = Date.now() + budgetMs;
  while ((await outstanding()) > 0 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 25));
  }
  await Promise.all(lanes.map((lane) => lane.stop()));
  // Said out loud: a lane that crashed on its first tick produces a perfect
  // `maxInFlight` of 0, and "nothing ran" must never read as "the limit held".
  for (const lane of lanes) expect(lane.crashedWith).toBeNull();
}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);
  root = await mkdtemp(join(tmpdir(), 'reup-dlconc-'));
  await cdn.start();
  cdn.respondWith(meter.responder);

  Object.assign(process.env, {
    API_KEY: 'download-concurrency-key-not-real',
    DATABASE_URL: schemaScopedDsn(),
    REDIS_URL: 'redis://localhost:6379/9',
    PARSER_BASE_URL: 'http://parser:80',
    PROBE_SEC_USER_ID: 'probe-sec-user-id',
    DATA_DIR: root,
    DOWNLOAD_CONCURRENCY: String(CONCURRENCY),
    // OFF: the lanes are started by hand below so nothing sweeps while this is
    // measuring, and so the reaper cannot hand a held-open download to a second
    // worker and inflate the count.
    LANES_ENABLED: 'false',
  });

  const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
    // The parser must never be reached: every `play_addr` seeded above is fresh
    // (F1), so a re-resolve here would mean the cache read is broken -- and a
    // stub that answered would hide that.
    .overrideProvider(PARSER_CLIENT)
    .useValue({
      fetchOneVideo: async () => {
        throw new Error('no re-resolve should happen: every seeded play_addr is fresh');
      },
      close: async () => {},
    })
    .compile();
  const app = moduleRef.createNestApplication();
  await app.init();
  productionLanes = moduleRef.get<DownloadLane[]>(DOWNLOAD_LANES);
  cdnClient = moduleRef.get<CdnClient>(CDN_CLIENT);
  storage = moduleRef.get<Storage>(STORAGE);
  // Assigned BEFORE the sanity assertion below, and that ordering is the fix for
  // a real observation: under the mutation that changes the array's length, the
  // assertion threw out of `beforeAll` while `close` was still undefined, so the
  // Nest app was never shut down, its pg pool stayed open, and jest HUNG instead
  // of reporting a failure. The mutation harness then logged "caught, but by
  // HANGING" -- a valid catch that names nothing. A teardown that only runs on
  // the happy path is not teardown.
  close = () => app.close();
  // Sanity: LANES resolved too, so the array under test is the one production
  // hands to the supervisor rather than a parallel construction.
  const all = moduleRef.get<LaneRunner[]>(LANES);
  expect(all.filter((lane) => lane instanceof DownloadLane)).toHaveLength(CONCURRENCY);
});

afterAll(async () => {
  await close?.();
  await cdn.stop();
  await pool.end();
  await dropSchema(SCHEMA);
  await rm(root, { recursive: true, force: true });
  process.env = { ...originalEnv };
});

describe('the download phase', () => {
  it(`has exactly DOWNLOAD_CONCURRENCY requests in flight at once, and never more`, async () => {
    meter.reset(CONCURRENCY);
    await truncate();
    await seedProfileAndVideos();

    await drain(productionLanes);

    // The work really happened -- otherwise the numbers below describe an idle
    // system.
    expect(await outstanding()).toBe(0);
    expect(meter.total).toBe(VIDEO_COUNT);
    // THE assertion, both halves at once. `=== CONCURRENCY` says the lanes do
    // reach the limit (so throughput is not silently 1) and that they never
    // exceed it (so the CDN is not being hit harder than §6.1 allows).
    expect(meter.maxInFlight).toBe(CONCURRENCY);

    // AND the N in flight were N DIFFERENT videos. "Three at once" would be
    // worthless if the three were the same one -- that is three writers on one
    // key, which is the phase-6 commit race rather than throughput. Files on
    // disk are the independent witness, and `meter.total === VIDEO_COUNT` above
    // is the other half: no video was fetched twice either.
    //
    // Asserted HERE rather than in a following `it`, because a second test
    // reading the first one's leftovers is state shared between cases: the
    // control below truncates, so the order would silently decide the result.
    const files = await readdir(join(root, 'douyin', PROFILE));
    expect(files.filter((f) => !f.endsWith('.tmp'))).toHaveLength(VIDEO_COUNT);
    expect(files.filter((f) => f.endsWith('.tmp'))).toEqual([]);
  });
});

describe('positive control: the instrument can see MORE than the limit', () => {
  it(`reports ${CONTROL_LANES} in flight when ${CONTROL_LANES} lanes really are running`, async () => {
    // Without this, `maxInFlight === 3` above is unfalsifiable: a counter that
    // is wired wrong, or a server that answers before its caller yields, reports
    // flawless compliance from a system with no limit at all.
    //
    // Lanes constructed HERE, deliberately not registered in the module -- this
    // control is about the measuring instrument, not about production wiring.
    meter.reset(CONTROL_LANES);
    await truncate();
    await seedProfileAndVideos();

    const extra = Array.from(
      { length: CONTROL_LANES },
      (_unused, i) =>
        new DownloadLane(quietLogger, db, parserStub(), cdnClient, storage, {
          index: 100 + i,
          leaseSeconds: 300,
          maxAttempts: 3,
          urlTtlSeconds: 7_200,
        }),
    );

    await drain(extra);

    expect(await outstanding()).toBe(0);
    expect(meter.maxInFlight).toBe(CONTROL_LANES);
  });
});

/**
 * The parser seam for the control's lanes.
 *
 * THROWS, exactly as the production override above does: every seeded
 * `play_addr` is fresh, so a re-resolve would mean F1's cache read is broken.
 * The control's lanes must differ from production's in exactly ONE respect --
 * how many there are -- so a stub that answered here would make the control
 * measure a different system.
 */
function parserStub() {
  return {
    fetchOneVideo: async () => {
      throw new Error('no re-resolve should happen: every seeded play_addr is fresh');
    },
  };
}
