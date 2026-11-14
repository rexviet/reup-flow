/**
 * The collection and glossary routes through a real Nest pipeline with the API key
 * guard, against a scratch database that holds both the downloader tables and
 * `localization.glossary`.
 */
import { INestApplication, Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import type { Pool } from 'pg';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { CollectionsController } from '../../src/collections/collections.controller';
import { CollectionsService } from '../../src/collections/collections.service';
import { ConfigModule } from '../../src/config/config.module';
import { PG_POOL } from '../../src/db/db.module';
import { channelCollection } from '../support/collections';
import { fixedEnv } from '../support/fixed-env';
import { createScratchDatabase, type ScratchDatabase } from '../support/localization-db';

jest.setTimeout(60_000);

const API_KEY = 'collections-http-e2e-key-not-real';
const MISSING = '00000000-0000-4000-8000-000000000000';

let scratch: ScratchDatabase;
let pool: Pool;
let app: INestApplication;

@Module({
  imports: [ConfigModule.withEnv(fixedEnv({ API_KEY }))],
  controllers: [CollectionsController],
  providers: [
    CollectionsService,
    { provide: PG_POOL, useFactory: () => pool },
    { provide: APP_GUARD, useClass: ApiKeyGuard },
  ],
})
class TestCollectionsModule {}

beforeAll(async () => {
  scratch = await createScratchDatabase();
  pool = scratch.pool;
  const moduleRef = await Test.createTestingModule({ imports: [TestCollectionsModule] }).compile();
  app = moduleRef.createNestApplication();
  await app.init();
}, 60_000);

afterAll(async () => {
  await app.close();
  await scratch.drop();
});

beforeEach(async () => {
  await pool.query(
    `TRUNCATE localization.glossary, folder_exports, dashboard_actions, videos, jobs, collections, profiles
       RESTART IDENTITY CASCADE`,
  );
});

const http = () => request(app.getHttpServer());
const send = (method: 'get' | 'post' | 'patch' | 'put' | 'delete', path: string, body?: unknown) => {
  const req = http()[method](path).set('X-Api-Key', API_KEY);
  return body === undefined ? req : req.send(body as object);
};

const video = async (awemeId: string, collectionId: string, secUserId = 'chA') =>
  pool.query(
    `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, collection_id) VALUES ($1, $2, now(), 'done', $3)`,
    [awemeId, secUserId, collectionId],
  );
const term = (collectionId: string, zh: string, vi: string, workflowId = 'video-1-g1', source = '1') =>
  pool.query(
    `INSERT INTO localization.glossary (collection_id, term_zh, term_vi, note, source_aweme_id, workflow_id)
     VALUES ($1, $2, $3, NULL, $4, $5)`,
    [collectionId, zh, vi, source, workflowId],
  );
const glossaryRows = async (collectionId: string) =>
  (
    await pool.query(
      `SELECT term_zh, term_vi, note, source_aweme_id, workflow_id FROM localization.glossary
        WHERE collection_id = $1 ORDER BY term_zh`,
      [collectionId],
    )
  ).rows;

describe('API key', () => {
  it.each([
    ['get', '/api/collections'],
    ['post', '/api/collections'],
    ['patch', `/api/collections/${MISSING}`],
    ['get', `/api/collections/${MISSING}/glossary`],
    ['put', `/api/collections/${MISSING}/glossary/x`],
    ['delete', `/api/collections/${MISSING}/glossary/x`],
  ] as const)('%s %s needs it', async (method, path) => {
    expect((await http()[method](path)).status).toBe(401);
  });
});

describe('GET /api/collections', () => {
  it('lists every collection with its video count, owning channel and settings', async () => {
    await pool.query(`INSERT INTO profiles (sec_user_id, nickname) VALUES ('chA', 'IM-薛珍珠')`);
    const channel = await channelCollection(pool, 'chA');
    const { rows } = await pool.query<{ id: string }>(
      `INSERT INTO collections (kind, external_id, sec_user_id, name, bgm_offset_db, caption_text_color,
                                cover_color, cover_opacity_pct, save_subdir)
       VALUES ('mix', 'mix1', 'chA', '成名的代价', -6.5, '#ffff00', '#000000', 55, 'Phim/成名的代价')
       RETURNING id`,
    );
    const mix = rows[0].id;
    const manual = (
      await pool.query<{ id: string }>(`INSERT INTO collections (kind, name) VALUES ('manual', 'Tự gom') RETURNING id`)
    ).rows[0].id;
    await video('1', mix);
    await video('2', mix);
    await video('3', channel);

    const res = await send('get', '/api/collections');
    expect(res.status).toBe(200);
    const byId = new Map(res.body.items.map((c: { id: string }) => [c.id, c]));
    expect(byId.get(mix)).toEqual({
      id: mix,
      kind: 'mix',
      external_id: 'mix1',
      name: '成名的代价',
      channel: { sec_user_id: 'chA', nickname: 'IM-薛珍珠' },
      video_count: 2,
      bgm_offset_db: -6.5,
      caption_text_color: '#ffff00',
      cover_color: '#000000',
      cover_opacity_pct: 55,
      save_subdir: 'Phim/成名的代价',
      created_at: expect.any(String),
      updated_at: expect.any(String),
    });
    expect(byId.get(channel)).toMatchObject({ kind: 'channel', video_count: 1, bgm_offset_db: null, save_subdir: null });
    expect(byId.get(manual)).toMatchObject({ kind: 'manual', channel: null, video_count: 0, external_id: null });
  });
});

describe('POST /api/collections', () => {
  it('creates a manual collection, storing colours in lower case', async () => {
    await pool.query(`INSERT INTO profiles (sec_user_id) VALUES ('chA')`);
    const res = await send('post', '/api/collections', {
      name: 'Phim hay',
      sec_user_id: 'chA',
      caption_text_color: '#FF0000',
      cover_opacity_pct: 40,
      save_subdir: 'Phim hay',
    });
    expect(res.status).toBe(201);
    expect(res.body).toMatchObject({
      kind: 'manual',
      external_id: null,
      name: 'Phim hay',
      channel: { sec_user_id: 'chA', nickname: null },
      video_count: 0,
      caption_text_color: '#ff0000',
      cover_opacity_pct: 40,
      save_subdir: 'Phim hay',
      bgm_offset_db: null,
    });
    const stored = await pool.query(`SELECT kind, caption_text_color FROM collections WHERE id = $1`, [res.body.id]);
    expect(stored.rows).toEqual([{ kind: 'manual', caption_text_color: '#ff0000' }]);
  });

  it('creates one without a channel', async () => {
    const res = await send('post', '/api/collections', { name: 'Không kênh' });
    expect(res.status).toBe(201);
    expect(res.body.channel).toBeNull();
  });

  it('answers 400, not 500, for a channel that has no profile, and stores nothing', async () => {
    const res = await send('post', '/api/collections', { name: 'X', sec_user_id: 'nobody' });
    expect(res.status).toBe(400);
    expect(JSON.stringify(res.body)).toContain('nobody');
    expect((await pool.query(`SELECT count(*)::int AS n FROM collections`)).rows[0].n).toBe(0);
  });

  it.each([
    { name: 'X', save_subdir: '../out' },
    { name: 'X', save_subdir: '/abs' },
    { name: 'X', caption_text_color: 'yellow' },
    { name: 'X', bgm_offset_db: 0.25 },
    { name: 'X', cover_opacity_pct: 101 },
    { name: 'X', kind: 'mix' },
    {},
  ])('rejects %j with 400 and stores nothing', async (body) => {
    expect((await send('post', '/api/collections', body)).status).toBe(400);
    expect((await pool.query(`SELECT count(*)::int AS n FROM collections`)).rows[0].n).toBe(0);
  });
});

describe('PATCH /api/collections/:id', () => {
  it('changes the name and settings, and null puts a setting back to the shared default', async () => {
    const id = (
      await pool.query<{ id: string }>(
        `INSERT INTO collections (kind, name, caption_text_color, bgm_offset_db, save_subdir, updated_at)
         VALUES ('manual', 'Cũ', '#ffffff', 3, 'old', now() - interval '1 day') RETURNING id`,
      )
    ).rows[0].id;
    const res = await send('patch', `/api/collections/${id}`, {
      name: 'Mới',
      caption_text_color: null,
      cover_color: '#00FF00',
      save_subdir: 'Phim/Mới',
    });
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({
      id,
      name: 'Mới',
      caption_text_color: null,
      cover_color: '#00ff00',
      bgm_offset_db: 3,
      save_subdir: 'Phim/Mới',
    });
    const row = (
      await pool.query(`SELECT caption_text_color, cover_color, bgm_offset_db, updated_at > now() - interval '1 minute' AS fresh
                          FROM collections WHERE id = $1`, [id])
    ).rows[0];
    expect(row).toEqual({ caption_text_color: null, cover_color: '#00ff00', bgm_offset_db: 3, fresh: true });
  });

  it('can edit a channel or 合集 collection too, without changing its kind', async () => {
    const id = await channelCollection(pool, 'chA');
    const res = await send('patch', `/api/collections/${id}`, { bgm_offset_db: -3.5 });
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ kind: 'channel', external_id: 'chA', bgm_offset_db: -3.5 });
  });

  it('answers 404 for a collection that does not exist and 400 for an id that is not a uuid', async () => {
    expect((await send('patch', `/api/collections/${MISSING}`, { name: 'x' })).status).toBe(404);
    expect((await send('patch', `/api/collections/not-a-uuid`, { name: 'x' })).status).toBe(400);
  });

  it.each([{}, { name: null }, { save_subdir: 'a/../b' }, { kind: 'manual' }])('rejects %j with 400', async (body) => {
    const id = await channelCollection(pool, 'chA');
    expect((await send('patch', `/api/collections/${id}`, body)).status).toBe(400);
  });
});

describe('collection glossary', () => {
  let a: string;
  let b: string;
  beforeEach(async () => {
    a = await channelCollection(pool, 'chA');
    b = await channelCollection(pool, 'chB');
  });

  it('GET lists only that collection, and says who set each term', async () => {
    await term(a, '苹果', 'Apple', 'video-7001-g1', '7001');
    await term(a, '华为', 'Huawei', 'dashboard', 'dashboard');
    await term(b, '小米', 'Xiaomi', 'video-7002-g1', '7002');

    const res = await send('get', `/api/collections/${a}/glossary`);
    expect(res.status).toBe(200);
    expect(res.body.items).toEqual([
      { term_zh: '华为', term_vi: 'Huawei', note: null, source: 'dashboard', source_aweme_id: null, created_at: expect.any(String) },
      { term_zh: '苹果', term_vi: 'Apple', note: null, source: 'agent', source_aweme_id: '7001', created_at: expect.any(String) },
    ]);
    const other = await send('get', `/api/collections/${b}/glossary`);
    expect(other.body.items.map((t: { term_zh: string }) => t.term_zh)).toEqual(['小米']);
  });

  it('GET answers 404 for a collection that does not exist', async () => {
    expect((await send('get', `/api/collections/${MISSING}/glossary`)).status).toBe(404);
  });

  it('PUT adds a term marked as set on the dashboard, in that collection only', async () => {
    const res = await send('put', `/api/collections/${a}/glossary/${encodeURIComponent('苹果')}`, {
      term_vi: ' Táo ',
      note: 'trái cây',
    });
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ term_zh: '苹果', term_vi: 'Táo', note: 'trái cây', source: 'dashboard' });
    expect(await glossaryRows(a)).toEqual([
      { term_zh: '苹果', term_vi: 'Táo', note: 'trái cây', source_aweme_id: 'dashboard', workflow_id: 'dashboard' },
    ]);
    expect(await glossaryRows(b)).toEqual([]);
  });

  it('PUT over a term the agent wrote replaces it and makes it a dashboard term', async () => {
    await term(a, '苹果', 'Apple', 'video-7001-g1', '7001');
    const res = await send('put', `/api/collections/${a}/glossary/${encodeURIComponent('苹果')}`, { term_vi: 'Táo' });
    expect(res.status).toBe(200);
    expect(await glossaryRows(a)).toEqual([
      { term_zh: '苹果', term_vi: 'Táo', note: null, source_aweme_id: 'dashboard', workflow_id: 'dashboard' },
    ]);
  });

  it.each([
    ['a link', { term_vi: 'ghé shop-xyz.vn' }, 'term_vi'],
    ['a phone number in the note', { term_vi: 'Táo', note: 'gọi 0912345678' }, 'note'],
    ['digits the Chinese term lacks', { term_vi: 'Táo 16' }, 'term_vi'],
    ['a line break', { term_vi: 'Tá\no' }, 'term_vi'],
    ['an empty value', { term_vi: '  ' }, 'term_vi'],
    ['an unknown key', { term_vi: 'Táo', source: 'agent' }, null],
  ])('PUT refuses %s with 400 and stores nothing', async (_label, body, field) => {
    const res = await send('put', `/api/collections/${a}/glossary/${encodeURIComponent('苹果')}`, body);
    expect(res.status).toBe(400);
    if (field) expect(JSON.stringify(res.body.message)).toContain(`${field}:`);
    expect(await glossaryRows(a)).toEqual([]);
  });

  it('PUT checks the Chinese term in the path too', async () => {
    const res = await send('put', `/api/collections/${a}/glossary/${encodeURIComponent('shop.vn')}`, { term_vi: 'cửa hàng' });
    expect(res.status).toBe(400);
    expect(JSON.stringify(res.body.message)).toContain('term_zh:');
  });

  it('PUT answers 404, not 500, for a collection that does not exist', async () => {
    const res = await send('put', `/api/collections/${MISSING}/glossary/${encodeURIComponent('苹果')}`, { term_vi: 'Táo' });
    expect(res.status).toBe(404);
  });

  it('DELETE removes the term from that collection only', async () => {
    await term(a, '苹果', 'Apple');
    await term(b, '苹果', 'Apple B');
    expect((await send('delete', `/api/collections/${a}/glossary/${encodeURIComponent('苹果')}`)).status).toBe(204);
    expect(await glossaryRows(a)).toEqual([]);
    expect((await glossaryRows(b)).map((r) => r.term_vi)).toEqual(['Apple B']);
  });

  it('PUT stores the terms in NFC, so a decomposed term overrides the row the agent wrote', async () => {
    await term(a, '豈', 'Apple', 'video-7001-g1', '7001');
    const res = await send('put', `/api/collections/${a}/glossary/${encodeURIComponent('豈')}`, {
      term_vi: 'Táo',
    });
    expect(res.status).toBe(200);
    expect(await glossaryRows(a)).toEqual([
      { term_zh: '豈', term_vi: 'Táo', note: null, source_aweme_id: 'dashboard', workflow_id: 'dashboard' },
    ]);
  });

  it('DELETE finds the term when the path names it in a non-NFC form', async () => {
    await term(a, '豈', 'Apple');
    expect((await send('delete', `/api/collections/${a}/glossary/${encodeURIComponent('豈')}`)).status).toBe(204);
    expect(await glossaryRows(a)).toEqual([]);
  });

  it('DELETE answers 404 for a term that is not there', async () => {
    expect((await send('delete', `/api/collections/${a}/glossary/${encodeURIComponent('苹果')}`)).status).toBe(404);
    expect((await send('delete', `/api/collections/${MISSING}/glossary/x`)).status).toBe(404);
  });
});
