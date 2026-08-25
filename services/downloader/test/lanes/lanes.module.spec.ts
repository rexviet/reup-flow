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
  it('contains exactly one reaper, and nothing else yet', () => {
    // "Exactly one" is not style. Approach 1's concurrency control is
    // structural (solution design §4): how many loops exist IS the limit. Two
    // reapers would double every sweep's contention for nothing, and F2's
    // "exactly one enumerate lane, globally" will be the same kind of
    // assertion here once phase 5 adds it.
    expect(lanes.filter((lane) => lane instanceof ReaperLane)).toHaveLength(1);
    expect(lanes).toHaveLength(1);
  });

  it('gives the reaper its 60-second cadence', () => {
    expect(lanes[0]!.idleDelayMs).toBe(REAP_INTERVAL_MS);
  });

  it('is the same array the supervisor watches', () => {
    // Two separate resolutions of one factory would leave the supervisor
    // starting lanes nobody can observe, and reporting staleness for lanes
    // nobody started.
    expect(supervisor.staleLanes().map((lane) => lane.name)).toEqual(['reaper']);
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
