/**
 * Phase 3 (group B) -- `jobs.controller.ts` exercised the way it actually
 * runs: through a real Nest HTTP pipeline, `ApiKeyGuard` registered as
 * `APP_GUARD` exactly like `app.module.ts` does, against a real per-run
 * Postgres schema and a real `FakeParser` standing in for the parser fork.
 *
 * `DB` and `PARSER_CLIENT` are swapped in directly (not via `DbModule`/
 * `ParserModule`) so this never opens a connection to the live
 * docker-compose stack's `public` schema -- see test/support/db.ts's note on
 * why that schema is off-limits to tests.
 */
import { INestApplication, Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import type { Pool } from 'pg';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule } from '../../src/config/config.module';
import { DB } from '../../src/db/db.module';
import { videos } from '../../src/db/schema';
import { JobsController } from '../../src/jobs/jobs.controller';
import { JobsService } from '../../src/jobs/jobs.service';
import { ParserClient } from '../../src/parser/parser.client';
import { PARSER_CLIENT } from '../../src/parser/parser.module';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';
import { fixedEnv } from '../support/fixed-env';
import { FakeParser } from '../support/fake-parser';

jest.setTimeout(30_000);

const SCHEMA = testSchemaName('jobs_http');
const API_KEY = 'jobs-http-e2e-test-key-not-real';

let pool: Pool;
let db: ReturnType<typeof testDb>;
let fakeParser: FakeParser;
let parserClient: ParserClient;
let app: INestApplication;

@Module({
  imports: [ConfigModule.withEnv(fixedEnv({ API_KEY }))],
  controllers: [JobsController],
  providers: [
    JobsService,
    // `db`/`parserClient` are assigned in beforeAll below; useFactory defers
    // reading them until Test.createTestingModule().compile() actually runs
    // (well after beforeAll has populated them), unlike useValue which would
    // capture `undefined` at class-decoration time.
    { provide: DB, useFactory: () => db },
    { provide: PARSER_CLIENT, useFactory: () => parserClient },
    { provide: APP_GUARD, useClass: ApiKeyGuard },
  ],
})
class TestJobsModule {}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);

  fakeParser = new FakeParser();
  await fakeParser.start();
  parserClient = new ParserClient({ baseUrl: fakeParser.baseUrl });

  const moduleRef = await Test.createTestingModule({ imports: [TestJobsModule] }).compile();
  app = moduleRef.createNestApplication();
  await app.init();
});

afterAll(async () => {
  await app.close();
  await parserClient.close();
  await fakeParser.stop();
  await pool.end();
  await dropSchema(SCHEMA);
});

afterEach(async () => {
  await truncateAll(pool);
  fakeParser.reset();
});

function authed(req: request.Test): request.Test {
  return req.set('X-Api-Key', API_KEY);
}

describe('POST /api/jobs', () => {
  it('requires X-Api-Key: missing header -> 401', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/jobs')
      .send({ url: 'https://www.douyin.com/user/sec-noauth' });
    expect(res.status).toBe(401);
  });

  it('requires X-Api-Key: wrong header -> 401', async () => {
    const res = await request(app.getHttpServer())
      .post('/api/jobs')
      .set('X-Api-Key', 'wrong-key-not-real')
      .send({ url: 'https://www.douyin.com/user/sec-noauth' });
    expect(res.status).toBe(401);
  });

  it('resolves a full profile URL and a short link to the SAME sec_user_id, forwarding each URL to the parser verbatim', async () => {
    fakeParser.respondWithEnvelope('sec-shared-shape');
    const fullUrl = 'https://www.douyin.com/user/sec-shared-shape';
    const shortUrl = 'https://v.douyin.com/aBcDeFg/';

    const first = await authed(request(app.getHttpServer()).post('/api/jobs')).send({ url: fullUrl });
    const second = await authed(request(app.getHttpServer()).post('/api/jobs')).send({ url: shortUrl });

    expect(first.status).toBe(201);
    expect(second.status).toBe(201);
    expect(first.body).toHaveProperty('job_id');
    expect(second.body).toHaveProperty('job_id');
    expect(first.body.job_id).not.toBe(second.body.job_id);

    // resolveSecUserId does no URL parsing on this side -- assert the client
    // forwarded each URL exactly as given, and both landed on one profile.
    expect(fakeParser.requests.map((r) => r.query.get('url'))).toEqual([fullUrl, shortUrl]);
    const rows = await pool.query('SELECT sec_user_id FROM jobs ORDER BY created_at');
    expect(rows.rows.map((r) => r.sec_user_id)).toEqual(['sec-shared-shape', 'sec-shared-shape']);
  });

  it('writes auto_localize on the channel, and leaves it alone when omitted', async () => {
    fakeParser.respondWithEnvelope('sec-switch');
    const url = 'https://www.douyin.com/user/sec-switch';
    const auto = async (): Promise<boolean> =>
      (await pool.query('SELECT auto_localize FROM profiles WHERE sec_user_id = $1', ['sec-switch'])).rows[0]
        .auto_localize;

    await authed(request(app.getHttpServer()).post('/api/jobs')).send({ url });
    expect(await auto()).toBe(false);

    const on = await authed(request(app.getHttpServer()).post('/api/jobs')).send({ url, auto_localize: true });
    expect(on.status).toBe(201);
    expect(await auto()).toBe(true);

    await authed(request(app.getHttpServer()).post('/api/jobs')).send({ url });
    expect(await auto()).toBe(true);

    await authed(request(app.getHttpServer()).post('/api/jobs')).send({ url, auto_localize: false });
    expect(await auto()).toBe(false);
  });

  it('rejects a non-boolean auto_localize with 400', async () => {
    const res = await authed(request(app.getHttpServer()).post('/api/jobs')).send({
      url: 'https://www.douyin.com/user/x',
      auto_localize: 'yes',
    });
    expect(res.status).toBe(400);
  });

  it('rejects a missing url with 400', async () => {
    const res = await authed(request(app.getHttpServer()).post('/api/jobs')).send({});
    expect(res.status).toBe(400);
  });

  it('maps a parser 5xx (ParserUnavailableError) to 503', async () => {
    fakeParser.respondWithParserError(500);
    const res = await authed(request(app.getHttpServer()).post('/api/jobs')).send({
      url: 'https://www.douyin.com/user/sec-unavailable',
    });
    expect(res.status).toBe(503);
  });

  it('maps a parser 400 (ParserRejectedError) to 502, never restating a diagnosis the fork never gave (F4)', async () => {
    fakeParser.respondWithParserError(400);
    const res = await authed(request(app.getHttpServer()).post('/api/jobs')).send({
      url: 'https://www.douyin.com/user/sec-rejected',
    });
    expect(res.status).toBe(502);
  });
});

describe('GET /api/jobs', () => {
  it('lists jobs newest first with pagination defaults and metadata', async () => {
    fakeParser.respondWithEnvelope('sec-page');
    for (let i = 0; i < 3; i++) {
      const created = await authed(request(app.getHttpServer()).post('/api/jobs')).send({
        url: `https://www.douyin.com/user/sec-page-${i}`,
      });
      await pool.query('UPDATE jobs SET created_at = $1 WHERE id = $2', [
        new Date(Date.UTC(2026, 0, 1) + i * 60_000),
        created.body.job_id,
      ]);
    }

    const res = await authed(request(app.getHttpServer()).get('/api/jobs'));

    expect(res.status).toBe(200);
    expect(res.body.total).toBe(3);
    expect(res.body.limit).toBe(20);
    expect(res.body.offset).toBe(0);
    expect(res.body.items).toHaveLength(3);
    expect(res.body.items[0]).toHaveProperty('job_id');
    // FIX 5 (review): `not.toHaveProperty('lease_expires_at')` only ever
    // checks the snake_case spelling. Drizzle's own row property is
    // `leaseExpiresAt` -- so a mapper that spread the ORM row (see
    // jobs.mapper.ts's comment on why it deliberately does not) would leak
    // `leaseExpiresAt` into the body and this assertion would never notice.
    // Confirmed by mutation: spreading the row in `toJobResponse` left this
    // exact assertion green while only the mapper's own unit spec went red.
    // Scanning the SERIALISED body for either spelling closes that gap.
    expect(JSON.stringify(res.body)).not.toMatch(/lease.?expires/i);
  });

  it('rejects an out-of-range limit with 400', async () => {
    const res = await authed(request(app.getHttpServer()).get('/api/jobs?limit=1000'));
    expect(res.status).toBe(400);
  });
});

describe('GET /api/jobs/:id', () => {
  it('404s for an id that does not exist', async () => {
    const res = await authed(
      request(app.getHttpServer()).get('/api/jobs/550e8400-e29b-41d4-a716-446655440000'),
    );
    expect(res.status).toBe(404);
  });

  it('400s for an id that is not a UUID, instead of reaching Postgres as 22P02', async () => {
    const res = await authed(request(app.getHttpServer()).get('/api/jobs/not-a-uuid'));
    expect(res.status).toBe(400);
  });

  it('returns job detail plus profile_video_counts (F7), never lease_expires_at', async () => {
    fakeParser.respondWithEnvelope('sec-detail');
    const created = await authed(request(app.getHttpServer()).post('/api/jobs')).send({
      url: 'https://www.douyin.com/user/sec-detail',
    });
    await db
      .insert(videos)
      .values({ awemeId: 'detail-v0', secUserId: 'sec-detail', createTime: new Date(), status: 'done' });

    const res = await authed(request(app.getHttpServer()).get(`/api/jobs/${created.body.job_id}`));

    expect(res.status).toBe(200);
    expect(res.body.job_id).toBe(created.body.job_id);
    expect(res.body.profile_video_counts).toEqual({ done: 1 });
    // See the comment on the same assertion in the `GET /api/jobs` test
    // above -- `leaseExpiresAt` (Drizzle's spelling) is what a spread would
    // actually leak, not `lease_expires_at`.
    expect(JSON.stringify(res.body)).not.toMatch(/lease.?expires/i);
  });
});

describe('POST /api/jobs/:id/cancel', () => {
  it('cancels a pending job', async () => {
    fakeParser.respondWithEnvelope('sec-cancel');
    const created = await authed(request(app.getHttpServer()).post('/api/jobs')).send({
      url: 'https://www.douyin.com/user/sec-cancel',
    });

    const res = await authed(request(app.getHttpServer()).post(`/api/jobs/${created.body.job_id}/cancel`));

    expect(res.status).toBe(200);
    expect(res.body.status).toBe('cancelled');
  });

  it('404s for an unknown id', async () => {
    const res = await authed(
      request(app.getHttpServer()).post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/cancel'),
    );
    expect(res.status).toBe(404);
  });

  it('409s when the job is already terminal', async () => {
    fakeParser.respondWithEnvelope('sec-terminal-cancel');
    const created = await authed(request(app.getHttpServer()).post('/api/jobs')).send({
      url: 'https://www.douyin.com/user/sec-terminal-cancel',
    });
    await pool.query("UPDATE jobs SET status = 'completed' WHERE id = $1", [created.body.job_id]);

    const res = await authed(request(app.getHttpServer()).post(`/api/jobs/${created.body.job_id}/cancel`));

    expect(res.status).toBe(409);
  });
});

describe('POST /api/jobs/:id/retry', () => {
  it('resets a blocked/failed job and its failed videos, reporting videos_reset', async () => {
    fakeParser.respondWithEnvelope('sec-retry');
    const created = await authed(request(app.getHttpServer()).post('/api/jobs')).send({
      url: 'https://www.douyin.com/user/sec-retry',
    });
    await pool.query("UPDATE jobs SET status = 'failed' WHERE id = $1", [created.body.job_id]);
    await db
      .insert(videos)
      .values({ awemeId: 'retry-v0', secUserId: 'sec-retry', createTime: new Date(), status: 'failed', attempts: 3 });

    const res = await authed(request(app.getHttpServer()).post(`/api/jobs/${created.body.job_id}/retry`));

    expect(res.status).toBe(200);
    expect(res.body.job.status).toBe('pending');
    expect(res.body.videos_reset).toBe(1);
  });

  it('404s for an unknown id', async () => {
    const res = await authed(
      request(app.getHttpServer()).post('/api/jobs/550e8400-e29b-41d4-a716-446655440000/retry'),
    );
    expect(res.status).toBe(404);
  });

  it('409s when the job is not blocked or failed', async () => {
    fakeParser.respondWithEnvelope('sec-not-retryable');
    const created = await authed(request(app.getHttpServer()).post('/api/jobs')).send({
      url: 'https://www.douyin.com/user/sec-not-retryable',
    });

    const res = await authed(request(app.getHttpServer()).post(`/api/jobs/${created.body.job_id}/retry`));

    expect(res.status).toBe(409);
  });
});
