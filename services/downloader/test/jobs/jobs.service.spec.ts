/**
 * Phase 3 (group B) -- `JobsService`, driven directly (no Nest DI, no HTTP)
 * against a real Postgres schema and a real `FakeParser`. Repository-style
 * setup on purpose: `createFromUrl`'s contract (resolve -> upsert -> insert,
 * one transaction, then stop) is a database property as much as it is a
 * service property, and a mocked `DB` would prove only that the mock agrees
 * with itself.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { JobsService } from '../../src/jobs/jobs.service';
import { ParserClient } from '../../src/parser/parser.client';
import { ParserRejectedError, ParserUnavailableError } from '../../src/parser/parser.errors';
import { jobs, profiles, videos } from '../../src/db/schema';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';
import { FakeParser } from '../support/fake-parser';

jest.setTimeout(30_000);

const SCHEMA = testSchemaName('jobs_service');

let pool: Pool;
let db: ReturnType<typeof testDb>;
let fakeParser: FakeParser;
let parserClient: ParserClient;
let service: JobsService;

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);

  fakeParser = new FakeParser();
  await fakeParser.start();
  parserClient = new ParserClient({ baseUrl: fakeParser.baseUrl });
  service = new JobsService(db, parserClient);
});

afterAll(async () => {
  await parserClient.close();
  await fakeParser.stop();
  await pool.end();
  await dropSchema(SCHEMA);
});

afterEach(async () => {
  await truncateAll(pool);
  fakeParser.reset();
});

describe('createFromUrl', () => {
  it('resolves sec_user_id through the parser, upserts the profile, and inserts a pending job -- then stops (plan §0 principle 1: no lane call)', async () => {
    fakeParser.respondWithEnvelope('sec-abc');

    const job = await service.createFromUrl('https://www.douyin.com/user/sec-abc');

    expect(job.status).toBe('pending');
    expect(job.secUserId).toBe('sec-abc');
    const [profileRow] = await db.select().from(profiles).where(eq(profiles.secUserId, 'sec-abc'));
    expect(profileRow).toBeDefined();
    const [jobRow] = await db.select().from(jobs).where(eq(jobs.id, job.id));
    expect(jobRow?.status).toBe('pending');
  });

  it('a full profile URL and a short link that resolve to the same sec_user_id land on the same profile row, and the client forwards each URL verbatim', async () => {
    fakeParser.respondWithEnvelope('sec-shared');
    const fullUrl = 'https://www.douyin.com/user/sec-shared';
    const shortUrl = 'https://v.douyin.com/aBcDeFg/';

    const first = await service.createFromUrl(fullUrl);
    const second = await service.createFromUrl(shortUrl);

    expect(first.secUserId).toBe('sec-shared');
    expect(second.secUserId).toBe('sec-shared');
    expect(first.id).not.toBe(second.id);
    // resolveSecUserId does no URL parsing on this side (the fork follows
    // short-link redirects itself) -- the client must hand the URL to the
    // parser exactly as given, not a reshaped or normalised version of it.
    const urlsSent = fakeParser.requests.map((r) => r.query.get('url'));
    expect(urlsSent).toEqual([fullUrl, shortUrl]);
    const profileRows = await db.select().from(profiles);
    expect(profileRows).toHaveLength(1);
  });

  it('propagates ParserUnavailableError untouched -- mapping to an HTTP status is the controller layer, not the service', async () => {
    fakeParser.respondWithParserError(500);

    await expect(service.createFromUrl('https://www.douyin.com/user/x')).rejects.toBeInstanceOf(
      ParserUnavailableError,
    );
  });

  it('propagates ParserRejectedError untouched', async () => {
    fakeParser.respondWithParserError(400);

    await expect(service.createFromUrl('https://www.douyin.com/user/x')).rejects.toBeInstanceOf(
      ParserRejectedError,
    );
  });
});

describe('retry', () => {
  it('resets the job and its failed videos together, and reports how many videos were reset', async () => {
    fakeParser.respondWithEnvelope('sec-retry');
    const job = await service.createFromUrl('https://www.douyin.com/user/sec-retry');
    await pool.query("UPDATE jobs SET status = 'failed' WHERE id = $1", [job.id]);
    await db.insert(videos).values([
      { awemeId: 'r0', secUserId: 'sec-retry', createTime: new Date(), status: 'failed', attempts: 3 },
      { awemeId: 'r1', secUserId: 'sec-retry', createTime: new Date(), status: 'done' },
    ]);

    const result = await service.retry(job.id);

    expect(result?.job.status).toBe('pending');
    expect(result?.videosReset).toBe(1);
  });

  it('returns null when the job is not in a retryable state', async () => {
    fakeParser.respondWithEnvelope('sec-not-retryable');
    const job = await service.createFromUrl('https://www.douyin.com/user/sec-not-retryable');

    expect(await service.retry(job.id)).toBeNull();
  });
});

describe('cancel', () => {
  it('cancels a pending job', async () => {
    fakeParser.respondWithEnvelope('sec-cancel');
    const job = await service.createFromUrl('https://www.douyin.com/user/sec-cancel');

    const cancelled = await service.cancel(job.id);

    expect(cancelled?.status).toBe('cancelled');
  });

  it('returns null when the job is already terminal', async () => {
    fakeParser.respondWithEnvelope('sec-terminal');
    const job = await service.createFromUrl('https://www.douyin.com/user/sec-terminal');
    await pool.query("UPDATE jobs SET status = 'completed' WHERE id = $1", [job.id]);

    expect(await service.cancel(job.id)).toBeNull();
  });
});

describe('list / findById / profileVideoCounts', () => {
  it('lists jobs and finds one by id', async () => {
    fakeParser.respondWithEnvelope('sec-list');
    const job = await service.createFromUrl('https://www.douyin.com/user/sec-list');

    const page = await service.list({ limit: 20, offset: 0 });
    expect(page.items.map((j) => j.id)).toContain(job.id);
    expect(await service.findById(job.id)).toEqual(job);
    expect(await service.findById('00000000-0000-0000-0000-000000000000')).toBeNull();
  });

  it('counts videos by status for a profile (F7)', async () => {
    fakeParser.respondWithEnvelope('sec-counts');
    await service.createFromUrl('https://www.douyin.com/user/sec-counts');
    await db.insert(videos).values({ awemeId: 'c0', secUserId: 'sec-counts', createTime: new Date(), status: 'done' });

    expect(await service.profileVideoCounts('sec-counts')).toEqual({ done: 1 });
  });
});
