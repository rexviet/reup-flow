/**
 * `PATCH /api/profiles/:secUserId` through a real Nest pipeline with the API key
 * guard, against a per-run Postgres schema.
 */
import { INestApplication, Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import type { Pool } from 'pg';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule } from '../../src/config/config.module';
import { ProfilesController } from '../../src/dashboard/profiles.controller';
import { DB } from '../../src/db/db.module';
import { localizationOutbox, profiles, videos } from '../../src/db/schema';
import { markDone } from '../../src/db/videos.repository';
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

jest.setTimeout(30_000);

const SCHEMA = testSchemaName('profiles_http');
const API_KEY = 'profiles-http-e2e-test-key-not-real';

let pool: Pool;
let db: ReturnType<typeof testDb>;
let app: INestApplication;

@Module({
  imports: [ConfigModule.withEnv(fixedEnv({ API_KEY }))],
  controllers: [ProfilesController],
  providers: [{ provide: DB, useFactory: () => db }, { provide: APP_GUARD, useClass: ApiKeyGuard }],
})
class TestProfilesModule {}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);
  const moduleRef = await Test.createTestingModule({ imports: [TestProfilesModule] }).compile();
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

const patch = (id: string, body: unknown, withKey = true): request.Test => {
  const req = request(app.getHttpServer()).patch(`/api/profiles/${id}`);
  return (withKey ? req.set('X-Api-Key', API_KEY) : req).send(body as object);
};

describe('PATCH /api/profiles/:secUserId', () => {
  it('requires X-Api-Key', async () => {
    await db.insert(profiles).values({ secUserId: 'p1' });
    expect((await patch('p1', { auto_localize: true }, false)).status).toBe(401);
  });

  it('flips the channel switch both ways', async () => {
    await db.insert(profiles).values({ secUserId: 'p1' });
    const on = await patch('p1', { auto_localize: true });
    expect(on.status).toBe(200);
    expect(on.body).toEqual({ sec_user_id: 'p1', auto_localize: true });
    expect((await db.select().from(profiles))[0]!.autoLocalize).toBe(true);

    await patch('p1', { auto_localize: false });
    expect((await db.select().from(profiles))[0]!.autoLocalize).toBe(false);
  });

  it('leaves already-held rows alone when the switch is turned on', async () => {
    await db.insert(profiles).values({ secUserId: 'p1' });
    await db.insert(videos).values({ awemeId: 'v1', secUserId: 'p1', createTime: new Date(), status: 'downloading' });
    await markDone(db, 'v1', 's3://bucket/v1.mp4');
    await patch('p1', { auto_localize: true });
    expect((await db.select().from(localizationOutbox))[0]!.state).toBe('held');
  });

  it('404s for an unknown channel', async () => {
    expect((await patch('nope', { auto_localize: true })).status).toBe(404);
  });

  it.each([['missing', {}], ['not a boolean', { auto_localize: 'yes' }]])(
    'rejects %s with 400',
    async (_label, body) => {
      await db.insert(profiles).values({ secUserId: 'p1' });
      expect((await patch('p1', body)).status).toBe(400);
    },
  );
});
