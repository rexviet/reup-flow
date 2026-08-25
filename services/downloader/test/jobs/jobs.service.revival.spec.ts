/**
 * PHASE 7 -- the service-level half of phase 6's handover.
 *
 * `test/db/cancel-retry-revival.spec.ts` proves `reviveSkippedForProfile` does
 * the right thing to rows. This proves the two operator actions actually CALL
 * it, which is a separate claim and the one that would silently not hold: the
 * repository function could be perfect and unreferenced, and every symptom of
 * the original bug would be back with a green suite over it.
 *
 * Both actions are checked in ONE transaction each, because a job that comes
 * back reporting "retried" while its videos stayed skipped is worse than a
 * failure -- it is a false all-clear, which is the family of bug this whole
 * phase is about.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { DB } from '../../src/db/db.module';
import * as jobsRepository from '../../src/db/jobs.repository';
import * as videosRepository from '../../src/db/videos.repository';
import { jobs, profiles, videos, type Video } from '../../src/db/schema';
import { JobsService } from '../../src/jobs/jobs.service';
import { PARSER_CLIENT } from '../../src/parser/parser.module';
import type { ParserClient } from '../../src/parser/parser.client';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';
import { Test } from '@nestjs/testing';

jest.setTimeout(60_000);

const SCHEMA = testSchemaName('jobs_revival');
const PROFILE = 'MS4wLjABAAAA_revival_service';
const PROFILE_URL = 'https://www.douyin.com/user/revival';

let pool: Pool;
let db: ReturnType<typeof testDb>;
let service: JobsService;

const parserStub = {
  resolveSecUserId: async () => PROFILE,
} as unknown as ParserClient;

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);

  const moduleRef = await Test.createTestingModule({
    providers: [
      JobsService,
      { provide: DB, useValue: db },
      { provide: PARSER_CLIENT, useValue: parserStub },
    ],
  }).compile();
  service = moduleRef.get(JobsService);
});

afterAll(async () => {
  await pool.end();
  await dropSchema(SCHEMA);
});

beforeEach(async () => {
  await db.insert(profiles).values({ secUserId: PROFILE }).onConflictDoNothing();
});

afterEach(async () => {
  await truncateAll(pool);
});

async function readVideo(awemeId: string): Promise<Video> {
  const [row] = await db.select().from(videos).where(eq(videos.awemeId, awemeId));
  if (!row) throw new Error(`video ${awemeId} not found`);
  return row;
}

/** A cancelled job plus one `skipped` video, reached through production calls. */
async function cancelledProfileWithSkippedVideo(awemeId: string): Promise<string> {
  const [job] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
  await pool.query(
    `INSERT INTO videos (aweme_id, sec_user_id, create_time) VALUES ($1, $2, now())`,
    [awemeId, PROFILE],
  );
  expect(await service.cancel(job!.id)).not.toBeNull();
  const claimed = await videosRepository.claimOne(db, { leaseSeconds: 300, maxAttempts: 3 });
  expect(await videosRepository.markSkipped(db, awemeId, claimed!.attempts)).toBe(true);
  return job!.id;
}

describe('JobsService.retry', () => {
  it('revives the profile skipped videos, not just its failed ones', async () => {
    const jobId = await cancelledProfileWithSkippedVideo('svc-skip');

    const result = await service.retry(jobId);

    expect(result).not.toBeNull();
    expect(result!.job.status).toBe('pending');
    expect((await readVideo('svc-skip')).status).toBe('pending');
    expect((await readVideo('svc-skip')).attempts).toBe(0);
  });

  it('counts BOTH kinds in `videosReset`, because that number is what the API reports', async () => {
    // `POST /retry` answers `{ videos_reset: n }`. A count that only saw
    // `failed` would tell an operator "1 video reset" while quietly reviving two
    // -- and, worse, would say "0 reset" for a purely-cancelled profile that had
    // in fact just been fully revived. Either way the number stops being usable
    // as confirmation, which is the only reason it is in the response.
    const jobId = await cancelledProfileWithSkippedVideo('svc-both-skip');
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, attempts, last_error)
       VALUES ('svc-both-failed', $1, now(), 'failed', 3, 'gave up')`,
      [PROFILE],
    );

    const result = await service.retry(jobId);

    expect(result!.videosReset).toBe(2);
    expect((await readVideo('svc-both-failed')).status).toBe('pending');
    expect((await readVideo('svc-both-skip')).status).toBe('pending');
  });

  it('does nothing at all when the job is not retryable', async () => {
    // The revival must be gated on the retry actually landing. A revival that
    // ran unconditionally would un-skip a profile whose cancel is still current,
    // and the very next lane tick would re-skip it -- a loop that spends
    // attempts and writes rows for no reason.
    const [job] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await pool.query("UPDATE jobs SET status = 'completed' WHERE id = $1", [job!.id]);
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, status)
       VALUES ('svc-untouched', $1, now(), 'skipped')`,
      [PROFILE],
    );

    expect(await service.retry(job!.id)).toBeNull();

    expect((await readVideo('svc-untouched')).status).toBe('skipped');
  });
});

describe('JobsService.createFromUrl', () => {
  it('revives a cancelled profile skipped videos, so the new job has real work', async () => {
    // Without this the new job would find nothing to do -- `upsertMany` is
    // `DO NOTHING` (F7) and `claimOne` never looks at `skipped` -- and because
    // `skipped` counts as settled it would report `completed` with zero
    // downloads and no error.
    await cancelledProfileWithSkippedVideo('svc-newjob');

    const created = await service.createFromUrl(PROFILE_URL);

    expect(created.status).toBe('pending');
    expect((await readVideo('svc-newjob')).status).toBe('pending');
    expect(await videosRepository.hasWorkForProfile(db, PROFILE)).toBe(true);
    // And with work outstanding, nothing can walk the new job to `completed`.
    await pool.query("UPDATE jobs SET status = 'downloading' WHERE id = $1", [created.id]);
    expect(await jobsRepository.finalizeSettled(db, { secUserId: PROFILE })).toHaveLength(0);
  });

  it('leaves `done` videos alone, so a re-run still only fetches what is missing', async () => {
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, storage_path)
       VALUES ('svc-done', $1, now(), 'done', 'p/q.mp4')`,
      [PROFILE],
    );

    await service.createFromUrl(PROFILE_URL);

    expect((await readVideo('svc-done')).status).toBe('done');
  });
});
