/**
 * Phase 6 -- the two job-level questions the download lane asks.
 *
 *   isProfileCancelled   step 2: "job của video này đã cancelled?"
 *   finalizeSettled      step 8: the last video for a profile flips its job to
 *                        `completed` -- the same statement the reaper already
 *                        runs as a safety net, now callable for ONE profile
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import {
  finalizeSettled,
  isProfileCancelled,
} from '../../src/db/jobs.repository';
import { jobs, profiles, videos, type Job } from '../../src/db/schema';
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

const SCHEMA = testSchemaName('download_finalize');
const PROFILE = 'MS4wLjABAAAA_dl_final';
const OTHER = 'MS4wLjABAAAA_dl_other';

let pool: Pool;
let db: ReturnType<typeof testDb>;

async function seedProfiles(): Promise<void> {
  await db
    .insert(profiles)
    .values([{ secUserId: PROFILE }, { secUserId: OTHER }])
    .onConflictDoNothing();
}

async function addJob(secUserId: string, status: string): Promise<string> {
  const [row] = await db.insert(jobs).values({ secUserId, status }).returning({ id: jobs.id });
  return row!.id;
}

async function addVideo(secUserId: string, awemeId: string, status: string): Promise<void> {
  await db.insert(videos).values({
    awemeId,
    secUserId,
    status,
    createTime: new Date(Date.UTC(2026, 0, 1)),
  });
}

async function readJob(id: string): Promise<Job> {
  const [row] = await db.select().from(jobs).where(eq(jobs.id, id));
  if (!row) throw new Error(`job ${id} not found`);
  return row;
}

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

afterEach(async () => {
  await truncateAll(pool);
});

describe('isProfileCancelled', () => {
  beforeEach(seedProfiles);

  it('is true when the profile\'s only job has been cancelled', async () => {
    await addJob(PROFILE, 'cancelled');

    expect(await isProfileCancelled(db, PROFILE)).toBe(true);
  });

  it('is FALSE when a cancelled job sits beside a live one', async () => {
    // The load-bearing case. `videos` has no `job_id` (F7), so "was my job
    // cancelled?" can only be answered from profile state -- and a profile
    // whose second job is still downloading very much still wants these bytes.
    // Reading "any cancelled job" as "stop" would have one cancel silently
    // skip every future job's videos for that profile.
    await addJob(PROFILE, 'cancelled');
    await addJob(PROFILE, 'downloading');

    expect(await isProfileCancelled(db, PROFILE)).toBe(false);
  });

  it.each(['pending', 'enumerating', 'downloading'])(
    'treats a %s job as live work',
    async (status) => {
      await addJob(PROFILE, 'cancelled');
      await addJob(PROFILE, status);

      expect(await isProfileCancelled(db, PROFILE)).toBe(false);
    },
  );

  it('is false when there is no cancelled job at all', async () => {
    await addJob(PROFILE, 'downloading');

    expect(await isProfileCancelled(db, PROFILE)).toBe(false);
  });

  it('is false for a profile with no jobs, rather than defaulting to "stop"', async () => {
    // A video with no job behind it should not exist, and if one somehow does,
    // downloading it is recoverable while marking it `skipped` is not: nothing
    // resets `skipped` (see `resetFailedForProfile`, which only touches
    // `failed`).
    expect(await isProfileCancelled(db, PROFILE)).toBe(false);
  });

  it('never answers about another profile\'s cancellation', async () => {
    await addJob(OTHER, 'cancelled');
    await addJob(PROFILE, 'downloading');

    expect(await isProfileCancelled(db, PROFILE)).toBe(false);
    expect(await isProfileCancelled(db, OTHER)).toBe(true);
  });
});

describe('finalizeSettled, scoped to one profile', () => {
  beforeEach(seedProfiles);

  it('completes the profile\'s downloading job once nothing is outstanding', async () => {
    const id = await addJob(PROFILE, 'downloading');
    await addVideo(PROFILE, 'a', 'done');
    await addVideo(PROFILE, 'b', 'failed');
    await addVideo(PROFILE, 'c', 'skipped');

    const finalized = await finalizeSettled(db, { secUserId: PROFILE });

    expect(finalized.map((job) => job.id)).toEqual([id]);
    const job = await readJob(id);
    expect(job.status).toBe('completed');
    expect(job.downloadedCount).toBe(1);
    expect(job.failedCount).toBe(1);
    expect(job.totalVideos).toBe(3);
  });

  it('declines while a single video is still outstanding', async () => {
    const id = await addJob(PROFILE, 'downloading');
    await addVideo(PROFILE, 'a', 'done');
    await addVideo(PROFILE, 'b', 'downloading');

    expect(await finalizeSettled(db, { secUserId: PROFILE })).toEqual([]);
    expect((await readJob(id)).status).toBe('downloading');
  });

  it('leaves other profiles\' finished jobs alone when scoped', async () => {
    // The lane calls this for the profile it just finished a video for. If the
    // scope leaked, one lane would be writing `completed` onto jobs it knows
    // nothing about -- which happens to be harmless (same predicate) and
    // therefore untestable later. Pinned now.
    const mine = await addJob(PROFILE, 'downloading');
    const theirs = await addJob(OTHER, 'downloading');
    await addVideo(PROFILE, 'a', 'done');
    await addVideo(OTHER, 'z', 'done');

    const finalized = await finalizeSettled(db, { secUserId: PROFILE });

    expect(finalized.map((job) => job.id)).toEqual([mine]);
    expect((await readJob(theirs)).status).toBe('downloading');
  });

  it('still sweeps every profile when called with no scope -- the reaper path', async () => {
    const mine = await addJob(PROFILE, 'downloading');
    const theirs = await addJob(OTHER, 'downloading');
    await addVideo(PROFILE, 'a', 'done');
    await addVideo(OTHER, 'z', 'done');

    const finalized = await finalizeSettled(db);

    expect(finalized.map((job) => job.id).sort()).toEqual([mine, theirs].sort());
  });
});
