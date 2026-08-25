/**
 * Phase 4 §4.3 -- the reaper: one lane, 60s cadence, four sweeps.
 *
 * Every one of the four exists because the failure it prevents is SILENT.
 * Nothing crashes, nothing alerts, nothing appears in a log; work simply
 * stops happening.
 *
 *   F3  a video stuck in `downloading` is invisible to the claim query, which
 *       only ever looks at `pending`.
 *   F5  a job stuck in `enumerating` is invisible to the job claim query for
 *       the same reason -- and brainstorm missed this one entirely, which is
 *       why plan §2 flags it as "a bug if left to the end".
 *   finalize  a job whose videos have all settled but whose status nobody
 *       flipped shows as a progress bar frozen at 100% forever.
 *   .tmp  a killed worker's temp file is a full-size video nothing will ever
 *       delete, invisible to `exists()` and eating the space `freeBytes()`
 *       promises the phase-10 preflight.
 *
 * Driven entirely through `runOnce()` called by hand -- no scheduler, no Nest
 * container. That is reason 3 for choosing Approach 1 (solution design §4) and
 * it is what makes plan §5's reaper mutations cheap to write.
 */
import { mkdir, mkdtemp, readdir, rm, utimes, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { claimOne as claimVideo } from '../../src/db/videos.repository';
import { claimOne as claimJob } from '../../src/db/jobs.repository';
import { jobs, profiles, videos, type Job, type Video } from '../../src/db/schema';
import { REAP_INTERVAL_MS, ReaperLane } from '../../src/lanes/reaper.lane';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import type { Storage } from '../../src/storage/storage.interface';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';

jest.setTimeout(60_000);

const SCHEMA = testSchemaName('reaper');
const LEASE_SECONDS = 300;
const MAX_ATTEMPTS = 3;
const PROFILE = 'MS4wLjABAAAA_reaper';

let pool: Pool;
let db: ReturnType<typeof testDb>;
let root: string;
let storage: LocalDiskStorage;
let lane: ReaperLane;
const logged: string[] = [];

const recordingLogger = {
  error: (m: string) => logged.push(`ERROR ${m}`),
  warn: (m: string) => logged.push(`WARN ${m}`),
  log: (m: string) => logged.push(`LOG ${m}`),
};

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);
});

afterAll(async () => {
  await pool.end();
  await dropSchema(SCHEMA);
});

beforeEach(async () => {
  root = await mkdtemp(join(tmpdir(), 'reup-reaper-'));
  storage = new LocalDiskStorage(root);
  logged.length = 0;
  lane = new ReaperLane(recordingLogger, db, storage, {
    leaseSeconds: LEASE_SECONDS,
    maxAttempts: MAX_ATTEMPTS,
  });
});

afterEach(async () => {
  await lane.stop();
  await truncateAll(pool);
  await rm(root, { recursive: true, force: true });
});

async function seedProfile(): Promise<void> {
  await db.insert(profiles).values({ secUserId: PROFILE }).onConflictDoNothing();
}

async function seedVideo(awemeId: string, status = 'pending'): Promise<void> {
  await seedProfile();
  await db.insert(videos).values({
    awemeId,
    secUserId: PROFILE,
    createTime: new Date(Date.UTC(2026, 0, 1)),
    status,
  });
}

async function readVideo(awemeId: string): Promise<Video> {
  const [row] = await db.select().from(videos).where(eq(videos.awemeId, awemeId));
  if (!row) throw new Error(`video ${awemeId} not found`);
  return row;
}

async function readJob(id: string): Promise<Job> {
  const [row] = await db.select().from(jobs).where(eq(jobs.id, id));
  if (!row) throw new Error(`job ${id} not found`);
  return row;
}

/** Simulates the worker dying and the lease elapsing, without sleeping. */
async function expireVideoLease(awemeId: string): Promise<void> {
  await pool.query(
    "UPDATE videos SET lease_expires_at = now() - interval '1 hour' WHERE aweme_id = $1",
    [awemeId],
  );
}

async function expireJobLease(id: string): Promise<void> {
  await pool.query(
    "UPDATE jobs SET lease_expires_at = now() - interval '1 hour' WHERE id = $1",
    [id],
  );
}

describe('cadence (§4.3)', () => {
  it('ticks every 60 seconds and always reports idle', async () => {
    // `'idle'` unconditionally, even on a sweep that reclaimed something. The
    // reaper is a heartbeat, not a queue consumer: reporting `'worked'` would
    // make the base class come straight back for another pass, and a second
    // pass one millisecond later can by definition find nothing (no lease
    // expires in that window). A fixed cadence is the honest shape.
    expect(lane.idleDelayMs).toBe(REAP_INTERVAL_MS);
    expect(REAP_INTERVAL_MS).toBe(60_000);
    await expect(lane.runOnce(new AbortController().signal)).resolves.toBe('idle');
  });

  it('is exactly one lane -- name included, so the staleness alert can name it', () => {
    expect(lane.name).toBe('reaper');
  });
});

describe('F3 -- a video whose worker died goes back to pending', () => {
  it('recovers a downloading video with an expired lease', async () => {
    await seedVideo('v-orphan');
    const claimed = await claimVideo(db, { leaseSeconds: LEASE_SECONDS, maxAttempts: MAX_ATTEMPTS });
    expect(claimed?.status).toBe('downloading');
    await expireVideoLease('v-orphan');

    await lane.runOnce(new AbortController().signal);

    const row = await readVideo('v-orphan');
    expect(row.status).toBe('pending');
    expect(row.leaseExpiresAt).toBeNull();
  });

  it('makes the recovered video immediately claimable again', async () => {
    // The point of the sweep, stated as the thing a lane observes. Without it
    // the claim query -- which only looks at `pending` -- never sees this row
    // again, and the video is lost with no error anywhere.
    await seedVideo('v-orphan');
    await claimVideo(db, { leaseSeconds: LEASE_SECONDS, maxAttempts: MAX_ATTEMPTS });
    await expireVideoLease('v-orphan');

    await lane.runOnce(new AbortController().signal);

    const reclaimed = await claimVideo(db, {
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
    });
    expect(reclaimed?.awemeId).toBe('v-orphan');
  });

  it('leaves a downloading video with a LIVE lease alone', async () => {
    // An expired lease is the only evidence of death. Reaping a live claim
    // hands a row to a second worker while the first is still streaming to
    // it -- the two-writers-one-file race in the risk register.
    await seedVideo('v-live');
    await claimVideo(db, { leaseSeconds: LEASE_SECONDS, maxAttempts: MAX_ATTEMPTS });

    await lane.runOnce(new AbortController().signal);

    expect((await readVideo('v-live')).status).toBe('downloading');
  });

  it('gives up on a video that has spent its attempts, rather than looping forever (F6)', async () => {
    await seedVideo('v-doomed');
    for (let i = 0; i < MAX_ATTEMPTS; i++) {
      await claimVideo(db, { leaseSeconds: LEASE_SECONDS, maxAttempts: MAX_ATTEMPTS });
      await expireVideoLease('v-doomed');
      await lane.runOnce(new AbortController().signal);
    }

    const row = await readVideo('v-doomed');
    expect(row.status).toBe('failed');
    expect(row.attempts).toBe(MAX_ATTEMPTS);
  });
});

describe('F5 -- a job whose lane died mid-enumerate goes back to pending', () => {
  it('recovers an enumerating job with an expired lease', async () => {
    await seedProfile();
    const [inserted] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    const claimed = await claimJob(db, { leaseSeconds: LEASE_SECONDS });
    expect(claimed?.status).toBe('enumerating');
    await expireJobLease(inserted!.id);

    await lane.runOnce(new AbortController().signal);

    const row = await readJob(inserted!.id);
    expect(row.status).toBe('pending');
    expect(row.leaseExpiresAt).toBeNull();
  });

  it('keeps max_cursor, so enumerate resumes from the checkpoint instead of re-paging', async () => {
    // The reason F5 keeps the cursor rather than resetting the job: re-paging
    // a profile from zero costs requests against an IP-based rate limit (F2),
    // which is the budget the whole design is built around.
    await seedProfile();
    const [inserted] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await claimJob(db, { leaseSeconds: LEASE_SECONDS });
    await pool.query('UPDATE jobs SET max_cursor = 1234567890 WHERE id = $1', [inserted!.id]);
    await expireJobLease(inserted!.id);

    await lane.runOnce(new AbortController().signal);

    const row = await readJob(inserted!.id);
    expect(row.status).toBe('pending');
    expect(row.maxCursor).toBe(1234567890);
  });

  it('leaves an enumerating job with a LIVE lease alone', async () => {
    await seedProfile();
    const [inserted] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await claimJob(db, { leaseSeconds: LEASE_SECONDS });

    await lane.runOnce(new AbortController().signal);

    expect((await readJob(inserted!.id)).status).toBe('enumerating');
  });
});

describe('finalize -- the safety net for phase 6 step 8', () => {
  it('completes a downloading job whose videos have all settled', async () => {
    await seedVideo('v-done', 'done');
    const [inserted] = await db
      .insert(jobs)
      .values({ secUserId: PROFILE, status: 'downloading' })
      .returning();

    await lane.runOnce(new AbortController().signal);

    expect((await readJob(inserted!.id)).status).toBe('completed');
  });

  it('does NOT complete a job that still has outstanding videos', async () => {
    await seedVideo('v-waiting', 'pending');
    const [inserted] = await db
      .insert(jobs)
      .values({ secUserId: PROFILE, status: 'downloading' })
      .returning();

    await lane.runOnce(new AbortController().signal);

    expect((await readJob(inserted!.id)).status).toBe('downloading');
  });
});

describe('.tmp sweep (plan §6 risk register, assigned to phase 4)', () => {
  /** Writes a temp file aged `ageMs` by mtime, shaped like `put`'s. */
  async function seedTemp(name: string, ageMs: number): Promise<string> {
    const dir = join(root, 'douyin', PROFILE);
    await mkdir(dir, { recursive: true });
    const path = join(dir, name);
    await writeFile(path, Buffer.alloc(32, 9));
    const when = new Date(Date.now() - ageMs);
    await utimes(path, when, when);
    return path;
  }

  async function listing(): Promise<string[]> {
    try {
      return (await readdir(join(root, 'douyin', PROFILE))).sort();
    } catch {
      return [];
    }
  }

  it('removes a temp file abandoned for longer than a full lease', async () => {
    await seedTemp('1.mp4.uuid.tmp', LEASE_SECONDS * 1000 + 60_000);

    await lane.runOnce(new AbortController().signal);

    expect(await listing()).toEqual([]);
  });

  it('keeps a temp file abandoned for LESS than a lease -- its worker may still be alive', async () => {
    // The cutoff is one lease, not an arbitrary number: it is the same
    // evidence the row sweeps act on, read off the filesystem. A worker whose
    // lease has not yet expired has not been reaped, so its bytes are not
    // garbage.
    await seedTemp('1.mp4.uuid.tmp', 10_000);

    await lane.runOnce(new AbortController().signal);

    expect(await listing()).toEqual(['1.mp4.uuid.tmp']);
  });

  it('logs what it reclaimed, so the space shows up somewhere an operator reads', async () => {
    await seedTemp('1.mp4.uuid.tmp', LEASE_SECONDS * 1000 + 60_000);

    await lane.runOnce(new AbortController().signal);

    expect(logged.join(' ')).toMatch(/tmp/i);
  });
});

describe('sustained survival (§4.2 DoD: runs continuously, random exceptions, stays alive)', () => {
  it('keeps ticking through repeated failures and lastTickAt keeps advancing', async () => {
    // The DoD asks for 60 seconds of this. Sixty real seconds in a unit suite
    // buys nothing that a fast loop over the same code path does not, so the
    // cadence is compressed and the SUBSTANCE is kept: the real ReaperLane,
    // its real sweeps, failing at random, over many ticks.
    //
    // Distinct from the base class's own survival tests: those drive a scripted
    // lane, and this drives the aggregation in `ReaperLane.runOnce` -- which
    // THROWS on any failed sweep and so is the reaper's own way of reaching the
    // guard. A `runOnce` that swallowed its failures would pass the base tests
    // and hide every reaper problem forever.
    let calls = 0;
    const flaky = {
      ...storage,
      removeStaleTemp: () => {
        calls += 1;
        return calls % 2 === 0
          ? Promise.reject(new Error('intermittent disk fault'))
          : Promise.resolve([]);
      },
    } as unknown as Storage;
    const flakyLane = new ReaperLane(recordingLogger, db, flaky, {
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
    });
    // Compressed cadence, same code path.
    Object.assign(flakyLane, { idleDelayMs: 2, errorBackoffMs: 2, maxBackoffMs: 20 });

    flakyLane.start();
    const deadline = Date.now() + 1_500;
    while (calls < 20 && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 20));
    }
    const tickAtEnd = flakyLane.lastTickAt;
    await flakyLane.stop();

    expect(calls).toBeGreaterThanOrEqual(20);
    expect(flakyLane.crashedWith).toBeNull();
    expect(tickAtEnd).not.toBeNull();
    // Alive by the metric an alert reads, not merely alive by inspection.
    expect(flakyLane.isStale(tickAtEnd!.getTime() + 1_000)).toBe(false);
    // And it really did fail roughly half the time -- otherwise this proves
    // only that a healthy lane ticks.
    expect(logged.filter((l) => l.startsWith('ERROR')).length).toBeGreaterThan(5);
  });
});

describe('one broken sweep must not starve the others', () => {
  it('still reaps rows when the .tmp sweep throws, and reports the failure', async () => {
    // The phase's own theme applied to the reaper itself. Four sweeps in one
    // tick means one of them throwing could silently stop the other three
    // forever -- the same silent-death shape the loop's try/catch exists for,
    // one level down. So each sweep runs independently and the tick fails
    // afterwards, which is what makes the base class log it and back off.
    await seedVideo('v-orphan');
    await claimVideo(db, { leaseSeconds: LEASE_SECONDS, maxAttempts: MAX_ATTEMPTS });
    await expireVideoLease('v-orphan');
    const broken: Storage = {
      ...storage,
      removeStaleTemp: () => Promise.reject(new Error('disk exploded')),
    } as unknown as Storage;
    const laneWithBrokenDisk = new ReaperLane(recordingLogger, db, broken, {
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
    });

    await expect(
      laneWithBrokenDisk.runOnce(new AbortController().signal),
    ).rejects.toThrow(/disk exploded/);

    // The row sweep ran anyway -- that is the assertion that matters.
    expect((await readVideo('v-orphan')).status).toBe('pending');
  });

  it('runs the .tmp sweep even when a row sweep throws', async () => {
    const dir = join(root, 'douyin', PROFILE);
    await mkdir(dir, { recursive: true });
    const stale = join(dir, '1.mp4.uuid.tmp');
    await writeFile(stale, Buffer.alloc(8, 1));
    const when = new Date(Date.now() - (LEASE_SECONDS * 1000 + 60_000));
    await utimes(stale, when, when);

    // A db handle whose every statement fails: Postgres restarting, say. The
    // disk sweep has nothing to do with Postgres and must still happen.
    const brokenDb = {
      update: () => {
        throw new Error('postgres is down');
      },
      select: () => {
        throw new Error('postgres is down');
      },
    } as never;
    const laneWithBrokenDb = new ReaperLane(recordingLogger, brokenDb, storage, {
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
    });

    await expect(
      laneWithBrokenDb.runOnce(new AbortController().signal),
    ).rejects.toThrow(/postgres is down/);

    expect(await readdir(dir)).toEqual([]);
  });
});
