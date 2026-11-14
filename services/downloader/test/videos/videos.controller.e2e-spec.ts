/**
 * Phase 3 (group B) -- `videos.controller.ts`, same harness shape as
 * jobs.controller.e2e-spec.ts: a real Nest pipeline, `ApiKeyGuard` as
 * `APP_GUARD`, `DB` swapped for a per-run test schema so this never touches
 * the live docker-compose stack's `public` schema.
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
import { VideosController } from '../../src/videos/videos.controller';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';
import { channelCollection } from '../support/collections';
import { fixedEnv } from '../support/fixed-env';

jest.setTimeout(30_000);

const SCHEMA = testSchemaName('videos_http');
const API_KEY = 'videos-http-e2e-test-key-not-real';

let pool: Pool;
let db: ReturnType<typeof testDb>;
let app: INestApplication;

@Module({
  imports: [ConfigModule.withEnv(fixedEnv({ API_KEY }))],
  controllers: [VideosController],
  providers: [
    { provide: DB, useFactory: () => db },
    { provide: APP_GUARD, useClass: ApiKeyGuard },
  ],
})
class TestVideosModule {}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);

  const moduleRef = await Test.createTestingModule({ imports: [TestVideosModule] }).compile();
  app = moduleRef.createNestApplication();
  await app.init();
});

afterAll(async () => {
  await app.close();
  await pool.end();
  await dropSchema(SCHEMA);
});

afterEach(async () => {
  await truncateAll(pool);
});

function authed(req: request.Test): request.Test {
  return req.set('X-Api-Key', API_KEY);
}

describe('GET /api/profiles/:secUserId/videos', () => {
  it('requires X-Api-Key: missing header -> 401', async () => {
    const res = await request(app.getHttpServer()).get('/api/profiles/sec-1/videos');
    expect(res.status).toBe(401);
  });

  it('lists a profiles videos newest first, paginated, snake_case, never lease_expires_at', async () => {
    const sec1 = await channelCollection(pool, 'sec-1');
    const sec2 = await channelCollection(pool, 'sec-2');
    await db.insert(videos).values([
      { awemeId: 'v0', secUserId: 'sec-1', collectionId: sec1, createTime: new Date(Date.UTC(2026, 0, 1)) },
      { awemeId: 'v1', secUserId: 'sec-1', collectionId: sec1, createTime: new Date(Date.UTC(2026, 0, 2)) },
      { awemeId: 'other', secUserId: 'sec-2', collectionId: sec2, createTime: new Date(Date.UTC(2026, 0, 3)) },
    ]);

    const res = await authed(request(app.getHttpServer()).get('/api/profiles/sec-1/videos'));

    expect(res.status).toBe(200);
    expect(res.body.total).toBe(2);
    expect(res.body.limit).toBe(20);
    expect(res.body.offset).toBe(0);
    expect(res.body.items.map((v: { aweme_id: string }) => v.aweme_id)).toEqual(['v1', 'v0']);
    // FIX 5 (review): checked against the serialised body, not
    // `not.toHaveProperty('lease_expires_at')` -- that only ever catches the
    // snake_case spelling, while Drizzle's own row property is
    // `leaseExpiresAt`. A mapper that spread the ORM row (see
    // videos.mapper.ts's comment on why it deliberately does not) would leak
    // `leaseExpiresAt` and the old assertion would never notice.
    expect(JSON.stringify(res.body.items[0])).not.toMatch(/lease.?expires/i);
  });

  it('filters by status', async () => {
    const collectionId = await channelCollection(pool, 'sec-1');
    await db.insert(videos).values([
      { awemeId: 'v0', secUserId: 'sec-1', collectionId, createTime: new Date(), status: 'done' },
      { awemeId: 'v1', secUserId: 'sec-1', collectionId, createTime: new Date(), status: 'pending' },
    ]);

    const res = await authed(
      request(app.getHttpServer()).get('/api/profiles/sec-1/videos?status=done'),
    );

    expect(res.body.items.map((v: { aweme_id: string }) => v.aweme_id)).toEqual(['v0']);
  });

  it('rejects an out-of-range limit with 400', async () => {
    const res = await authed(request(app.getHttpServer()).get('/api/profiles/sec-1/videos?limit=1000'));
    expect(res.status).toBe(400);
  });
});
