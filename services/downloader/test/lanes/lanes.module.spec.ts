/**
 * Phase 4 -- what the PRODUCTION lane wiring actually contains.
 *
 * Everything else in test/lanes drives lanes the test constructed itself, and
 * `sigterm-shutdown.e2e-spec.ts` boots the real `AppModule` with the `LANES`
 * provider deliberately REPLACED. So nothing yet asserts the one fact
 * production depends on: that the array `LaneSupervisor` is handed contains the
 * reaper, configured from the environment.
 *
 * That is not a hypothetical gap. `LANES` is a string token filled by a
 * factory; a rename, a dropped entry, or a factory that resolves to `[]`
 * leaves a process that boots healthy, answers `/health`, and reaps nothing
 * ever -- the same silent-nothing-happens failure the whole phase is about.
 *
 * Resolved rather than run: this asks what the container built, and every
 * question about behaviour is answered elsewhere. Lanes stay OFF so nothing
 * starts a 60s sweep against the live compose stack.
 */
import { Test } from '@nestjs/testing';
import { AppModule } from '../../src/app.module';
import { DOWNLOAD_IDLE_MS, DownloadLane } from '../../src/lanes/download.lane';
import {
  ENUMERATE_IDLE_MS,
  EnumerateLane,
} from '../../src/lanes/enumerate.lane';
import type { LaneRunner } from '../../src/lanes/lane.runner';
import { LaneSupervisor } from '../../src/lanes/lane.supervisor';
import { LANES } from '../../src/lanes/lanes.tokens';
import { REAP_INTERVAL_MS, ReaperLane } from '../../src/lanes/reaper.lane';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import { STORAGE } from '../../src/storage/storage.module';

jest.setTimeout(60_000);

const originalEnv = { ...process.env };
let lanes: LaneRunner[];
let supervisor: LaneSupervisor;
let storage: unknown;
let close: (() => Promise<void>) | undefined;

beforeAll(async () => {
  Object.assign(process.env, {
    API_KEY: 'lanes-module-test-key-not-real',
    DATABASE_URL: 'postgres://reup:reup@localhost:5432/reup',
    // Redis logical database 9, never the default 0 -- the compose stack's
    // parser reads `douyin:cookie:current` from db 0 on this same server.
    REDIS_URL: 'redis://localhost:6379/9',
    PARSER_BASE_URL: 'http://parser:80',
    PROBE_SEC_USER_ID: 'probe-sec-user-id',
    // Deliberately NOT the default of 6: what is under test is that the array
    // is built from this variable, and a test run at the default cannot tell
    // "reads the env var" from "hard-codes 6".
    DOWNLOAD_CONCURRENCY: '4',
    // OFF: this suite inspects what the container built and must not start a
    // reaper sweeping the live stack's `public` schema as a side effect.
    LANES_ENABLED: 'false',
  });

  const moduleRef = await Test.createTestingModule({ imports: [AppModule] }).compile();
  const app = moduleRef.createNestApplication();
  await app.init();
  lanes = moduleRef.get<LaneRunner[]>(LANES);
  supervisor = moduleRef.get(LaneSupervisor);
  storage = moduleRef.get(STORAGE);
  close = () => app.close();
});

afterAll(async () => {
  await close?.();
  process.env = { ...originalEnv };
});

describe('the production LANES array', () => {
  it('contains exactly one reaper', () => {
    // "Exactly one" is not style. Approach 1's concurrency control is
    // structural (solution design §4): how many loops exist IS the limit. Two
    // reapers would double every sweep's contention for nothing.
    expect(lanes.filter((lane) => lane instanceof ReaperLane)).toHaveLength(1);
  });

  it('contains exactly ONE enumerate lane -- F2, for the whole system', () => {
    // The load-bearing assertion of phase 5, and the reason it lives in THIS
    // file rather than in the lane's own spec: F2 ("enumerate concurrency = 1
    // globally") is not a property of `EnumerateLane`, it is a property of this
    // array. Douyin rate-limits by IP, not by job, so three operators starting
    // three jobs must still produce one job's worth of requests -- and what
    // makes that true is that there is one loop, not a semaphore that could
    // miscount.
    //
    // A count is not the whole guarantee, though: this test would stay green if
    // the lane paged two jobs at once inside one loop. The REQUEST RATE is
    // measured in enumerate-global-concurrency.spec.ts, which drives this same
    // array.
    expect(lanes.filter((lane) => lane instanceof EnumerateLane)).toHaveLength(1);
  });

  it('contains exactly DOWNLOAD_CONCURRENCY download lanes -- §6.1', () => {
    // Concurrency IS the number of loops (solution design §4). There is no
    // semaphore to miscount and no runtime check: six concurrent downloads are
    // six entries in this array, which is why "how many at once?" is a diff.
    //
    // Six is safe where enumerate's one is not, and that is not a guess:
    // `*.douyinvod.com` serves bytes and is not behind the risk control that
    // rate-limits the web API by IP (IDEA.md §3.1 vs §3.2).
    expect(lanes.filter((lane) => lane instanceof DownloadLane)).toHaveLength(4);
  });

  it('contains nothing else', () => {
    // An extra entry is a mistake; an entry that vanished is a queue that
    // silently never drains.
    expect(lanes).toHaveLength(6);
  });

  it('gives every download lane its own name, so the staleness alert can name it', () => {
    // §4.1's alert says WHICH lane stopped ticking. Four identically-named
    // loops would report "a lane is stuck" -- and one wedged socket out of six
    // is exactly the case that alert exists for.
    const names = lanes.filter((l) => l instanceof DownloadLane).map((l) => l.name);
    expect(new Set(names).size).toBe(names.length);
    expect(names.sort()).toEqual(['download-1', 'download-2', 'download-3', 'download-4']);
  });

  it('gives the download lanes their 1-second idle poll', () => {
    for (const lane of lanes.filter((l) => l instanceof DownloadLane)) {
      expect(lane.idleDelayMs).toBe(DOWNLOAD_IDLE_MS);
    }
  });

  it('gives the reaper its 60-second cadence', () => {
    expect(lanes.find((l) => l instanceof ReaperLane)!.idleDelayMs).toBe(REAP_INTERVAL_MS);
  });

  it('gives the enumerate lane its 1-second idle poll', () => {
    // The queue is Postgres and the claim is a partial-index lookup, so polling
    // every second costs approximately nothing. This is NOT the pace of Douyin
    // requests -- that is ENUMERATE_SLEEP_MS_MIN/MAX, and it lives between
    // pages, inside the tick.
    expect(lanes.find((l) => l instanceof EnumerateLane)!.idleDelayMs).toBe(ENUMERATE_IDLE_MS);
  });

  it('is the same array the supervisor watches', () => {
    // Two separate resolutions of one factory would leave the supervisor
    // starting lanes nobody can observe, and reporting staleness for lanes
    // nobody started.
    expect(supervisor.staleLanes().map((lane) => lane.name).sort()).toEqual([
      'download-1',
      'download-2',
      'download-3',
      'download-4',
      'enumerate',
      'reaper',
    ]);
  });
});

describe('the production STORAGE provider', () => {
  it('is a LocalDiskStorage', () => {
    // ONE instance matters: it keeps an in-memory set of the temp files it is
    // currently writing, and that set is what makes the reaper's `.tmp` sweep
    // safe to run alongside live downloads. A second instance would know none
    // of the first's writes.
    expect(storage).toBeInstanceOf(LocalDiskStorage);
  });
});
