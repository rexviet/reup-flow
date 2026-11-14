/**
 * `GET`/`PUT /api/dashboard/auto-queue` through a real Nest pipeline with the API key guard,
 * against a scratch Postgres holding the real queue triggers.
 */
import { INestApplication, Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import type { Pool } from 'pg';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule } from '../../src/config/config.module';
import { AutoQueueController } from '../../src/dashboard/auto-queue.controller';
import { PG_POOL } from '../../src/db/db.module';
import { fixedEnv } from '../support/fixed-env';
import { createScratchDatabase, type ScratchDatabase } from '../support/localization-db';

jest.setTimeout(60_000);

const API_KEY = 'auto-queue-e2e-api-key-not-real';

let scratch: ScratchDatabase;
let pool: Pool;
let app: INestApplication;

beforeAll(async () => {
  scratch = await createScratchDatabase();
  pool = scratch.pool;
  @Module({
    imports: [ConfigModule.withEnv(fixedEnv({ API_KEY }))],
    controllers: [AutoQueueController],
    providers: [{ provide: PG_POOL, useValue: pool }, { provide: APP_GUARD, useClass: ApiKeyGuard }],
  })
  class TestAutoQueueModule {}
  const moduleRef = await Test.createTestingModule({ imports: [TestAutoQueueModule] }).compile();
  app = moduleRef.createNestApplication();
  await app.init();
});

afterAll(async () => {
  await app.close();
  await scratch.drop();
});

beforeEach(async () => {
  await pool.query(`TRUNCATE videos, profiles, localization_outbox, auto_queue_inflight, dashboard_actions CASCADE`);
  await pool.query(`UPDATE auto_queue_settings SET enabled = true`);
  await pool.query(`INSERT INTO profiles (sec_user_id) VALUES ('ch')`);
});

async function hold(id: string): Promise<void> {
  await pool.query(`INSERT INTO videos (aweme_id, sec_user_id, create_time, status) VALUES ($1, 'ch', now(), 'done')`, [id]);
  await pool.query(`INSERT INTO localization_outbox (aweme_id, state) VALUES ($1, 'held')`, [id]);
}

async function states(): Promise<Record<string, string>> {
  const { rows } = await pool.query<{ aweme_id: string; state: string }>(
    `SELECT aweme_id, state FROM localization_outbox ORDER BY aweme_id`,
  );
  return Object.fromEntries(rows.map((r) => [r.aweme_id, r.state]));
}

async function finish(id: string): Promise<void> {
  await pool.query(
    `INSERT INTO localization.runs (aweme_id, generation, workflow_id, status) VALUES ($1, 1, 'w', 'ready_for_review')`,
    [id],
  );
}

const put = (body: unknown, withKey = true): request.Test => {
  const req = request(app.getHttpServer()).put('/api/dashboard/auto-queue');
  return (withKey ? req.set('X-Api-Key', API_KEY) : req).send(body as object);
};

describe('auto-queue switch', () => {
  it('requires X-Api-Key', async () => {
    await request(app.getHttpServer()).get('/api/dashboard/auto-queue').expect(401);
    await put({ enabled: false }, false).expect(401);
  });

  it('reports the switch, the video in flight and how many are held', async () => {
    await hold('1');
    await hold('2');
    const res = await request(app.getHttpServer()).get('/api/dashboard/auto-queue').set('X-Api-Key', API_KEY).expect(200);
    expect(res.body).toMatchObject({ enabled: true, held: 1, in_flight: { aweme_id: '1', generation: 1 } });
  });

  it('off: a finished run starts nothing; on again: the next held video starts', async () => {
    await hold('1');
    await hold('2');
    const off = await put({ enabled: false }).expect(200);
    expect(off.body).toMatchObject({ enabled: false, held: 1 });

    await finish('1');
    expect(await states()).toEqual({ '1': 'pending', '2': 'held' });

    const on = await put({ enabled: true }).expect(200);
    expect(on.body).toMatchObject({ enabled: true, held: 0, in_flight: { aweme_id: '2' } });
    expect(await states()).toEqual({ '1': 'pending', '2': 'pending' });
  });

  it('rejects a body that is not just a boolean', async () => {
    await put({ enabled: 'yes' }).expect(400);
    await put({ enabled: true, extra: 1 }).expect(400);
  });
});
