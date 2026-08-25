/**
 * Phase 4 §4.3 -- `finalizeSettled`, the reaper's safety net for a job whose
 * work is all done but whose status nobody flipped.
 *
 * Phase 6 step 8 is the NORMAL path: the download lane that finishes the last
 * video for a profile marks the job `completed`. That step can lose. Two
 * workers finishing their last two videos at the same instant can both look,
 * both see one outstanding row (each other's), and both decline -- so nobody
 * finalizes. The job then sits at `downloading` forever with every video
 * `done`, which reads to an operator as a stuck job and to the UI as a
 * progress bar frozen at 100%.
 *
 * A separate spec file from jobs.repository.spec.ts because this needs
 * `videos` rows to set up, which that file (a pure jobs-table suite) has no
 * business seeding.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { finalizeSettled } from '../../src/db/jobs.repository';
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

const SCHEMA = testSchemaName('jobs_finalize');

let pool: Pool;
let db: ReturnType<typeof testDb>;

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

/** Creates a profile with a job in `status`, plus one video per entry of `videoStatuses`. */
async function seed(status: string, videoStatuses: string[]): Promise<string> {
  const secUserId = `profile-${status}-${videoStatuses.join('-') || 'empty'}`;
  await db.insert(profiles).values({ secUserId }).onConflictDoNothing();
  const [job] = await db.insert(jobs).values({ secUserId, status }).returning();
  if (videoStatuses.length > 0) {
    await db.insert(videos).values(
      videoStatuses.map((videoStatus, i) => ({
        awemeId: `${secUserId}-v${i}`,
        secUserId,
        createTime: new Date(Date.UTC(2026, 0, 1) + i * 1000),
        status: videoStatus,
      })),
    );
  }
  return job!.id;
}

async function readJob(id: string): Promise<Job> {
  const [row] = await db.select().from(jobs).where(eq(jobs.id, id));
  if (!row) throw new Error(`job ${id} not found`);
  return row;
}

describe('finalizeSettled', () => {
  it('completes a downloading job whose every video has settled', async () => {
    const jobId = await seed('downloading', ['done', 'done', 'failed', 'skipped']);

    const finalized = await finalizeSettled(db);

    expect(finalized.map((j) => j.id)).toEqual([jobId]);
    expect((await readJob(jobId)).status).toBe('completed');
  });

  it('completes a downloading job for a profile with NO videos at all', async () => {
    // An enumerate that legitimately found nothing (an empty or deleted
    // profile) still flips the job to `downloading` before it exits. With no
    // videos, phase 6's per-video finalize check never runs at all -- there is
    // no last download to trigger it -- so this is the ONLY thing that ever
    // finishes such a job.
    const jobId = await seed('downloading', []);

    await finalizeSettled(db);

    expect((await readJob(jobId)).status).toBe('completed');
  });

  it.each([['pending'], ['downloading']])(
    'leaves a downloading job alone while a video is still %s',
    async (outstanding) => {
      const jobId = await seed('downloading', ['done', outstanding]);

      expect(await finalizeSettled(db)).toEqual([]);
      expect((await readJob(jobId)).status).toBe('downloading');
    },
  );

  it.each([['pending'], ['enumerating'], ['blocked'], ['cancelled'], ['failed'], ['completed']])(
    'never touches a job in status %s',
    async (status) => {
      // Only `downloading` is finalizable. `enumerating` in particular must be
      // left to the F5 reap: a job whose lane died mid-enumerate has no videos
      // yet, so "every video has settled" is vacuously true and this would
      // mark a job complete that never enumerated a single page.
      const jobId = await seed(status, ['done']);

      expect(await finalizeSettled(db)).toEqual([]);
      expect((await readJob(jobId)).status).toBe(status);
    },
  );

  it('sets the counters from PROFILE state, so a completed job is not reported as 0 done (F7)', async () => {
    const jobId = await seed('downloading', ['done', 'done', 'done', 'failed', 'skipped']);

    await finalizeSettled(db);

    const job = await readJob(jobId);
    expect(job.downloadedCount).toBe(3);
    expect(job.failedCount).toBe(1);
    // Counted across the profile rather than across this job's own inserts --
    // see countByStatusForProfile in videos.repository.ts for why `videos` has
    // no `job_id` and why this is the honest number.
    expect(job.totalVideos).toBe(5);
  });

  it('bumps updated_at, so the UI sees the transition', async () => {
    const jobId = await seed('downloading', ['done']);
    const before = (await readJob(jobId)).updatedAt;
    await new Promise((r) => setTimeout(r, 10));

    await finalizeSettled(db);

    expect((await readJob(jobId)).updatedAt.getTime()).toBeGreaterThan(before.getTime());
  });

  it('is idempotent -- a second pass finds nothing', async () => {
    await seed('downloading', ['done']);

    expect(await finalizeSettled(db)).toHaveLength(1);
    expect(await finalizeSettled(db)).toEqual([]);
  });

  it('finalizes only the settled job when two profiles are in flight', async () => {
    const settledId = await seed('downloading', ['done']);
    const busyId = await seed('downloading', ['pending']);

    const finalized = await finalizeSettled(db);

    expect(finalized.map((j) => j.id)).toEqual([settledId]);
    expect((await readJob(busyId)).status).toBe('downloading');
  });
});
