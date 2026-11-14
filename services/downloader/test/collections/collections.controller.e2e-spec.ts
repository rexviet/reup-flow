/**
 * The collection and glossary routes through a real Nest pipeline with the API key
 * guard, against a scratch database that holds both the downloader tables and
 * `localization.glossary`.
 */
import { INestApplication, Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import type { Pool } from 'pg';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { CollectionsController } from '../../src/collections/collections.controller';
import { CollectionsService } from '../../src/collections/collections.service';
import { CoverPreviewService } from '../../src/collections/cover-preview';
import { ConfigModule, ENV } from '../../src/config/config.module';
import type { Env } from '../../src/config/env';
import { DashboardMediaService, MEDIA_READER, type MediaReader } from '../../src/dashboard/dashboard.media';
import { verifyMediaToken } from '../../src/dashboard/media-token';
import { PG_POOL } from '../../src/db/db.module';
import { channelCollection } from '../support/collections';
import { fixedEnv } from '../support/fixed-env';
import { createScratchDatabase, type ScratchDatabase } from '../support/localization-db';

jest.setTimeout(60_000);

const API_KEY = 'collections-http-e2e-key-not-real';
const MISSING = '00000000-0000-4000-8000-000000000000';
const SIGNING_KEY = 'c'.repeat(40);
const BUCKET = 'reup-media';

/** The object store, in memory: only the cover preview reads it. */
const objects = new Map<string, Buffer>();
const reader: MediaReader = {
  async open(key: string) {
    const data = objects.get(key);
    return data ? { status: 200 as const, body: Readable.from([data]), contentLength: data.length } : null;
  },
};

let scratch: ScratchDatabase;
let pool: Pool;
let app: INestApplication;

@Module({
  imports: [
    ConfigModule.withEnv(
      fixedEnv({ API_KEY, DASHBOARD_MEDIA_SECRET: SIGNING_KEY, STORAGE_BACKEND: 's3', S3_BUCKET: BUCKET }),
    ),
  ],
  controllers: [CollectionsController],
  providers: [
    CollectionsService,
    CoverPreviewService,
    { provide: PG_POOL, useFactory: () => pool },
    { provide: MEDIA_READER, useValue: reader },
    {
      provide: DashboardMediaService,
      inject: [PG_POOL, ENV, MEDIA_READER],
      useFactory: (p: Pool, e: Env, r: MediaReader | null) => new DashboardMediaService(p, e, r, Date.now),
    },
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
  objects.clear();
  await pool.query(
    `TRUNCATE localization.runs, localization.glossary, folder_exports, dashboard_actions, videos, jobs, collections, profiles
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
    ['get', `/api/collections/${MISSING}/cover-preview`],
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
                                cover_color, cover_opacity_pct, cover_offset_pct, cover_width_pct,
                                cover_height_pct, save_subdir)
       VALUES ('mix', 'mix1', 'chA', '成名的代价', -6.5, '#ffff00', '#000000', 55, 3, 100, 12, 'Phim/成名的代价')
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
      cover_offset_pct: 3,
      cover_width_pct: 100,
      cover_height_pct: 12,
      save_subdir: 'Phim/成名的代价',
      created_at: expect.any(String),
      updated_at: expect.any(String),
    });
    expect(byId.get(channel)).toMatchObject({
      kind: 'channel',
      video_count: 1,
      bgm_offset_db: null,
      save_subdir: null,
      cover_offset_pct: null,
      cover_width_pct: null,
      cover_height_pct: null,
    });
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
      cover_offset_pct: -50,
      cover_width_pct: 20,
      cover_height_pct: 50,
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
      cover_offset_pct: -50,
      cover_width_pct: 20,
      cover_height_pct: 50,
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
    { name: 'X', cover_offset_pct: 51 },
    { name: 'X', cover_width_pct: 19 },
    { name: 'X', cover_height_pct: 2.5 },
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

  it('sets the cover geometry, and null puts it back to automatic', async () => {
    const id = await channelCollection(pool, 'chA');
    const set = await send('patch', `/api/collections/${id}`, { cover_offset_pct: 3, cover_width_pct: 100 });
    expect(set.status).toBe(200);
    expect(set.body).toMatchObject({ cover_offset_pct: 3, cover_width_pct: 100, cover_height_pct: null });
    const cleared = await send('patch', `/api/collections/${id}`, { cover_offset_pct: null, cover_height_pct: 12 });
    expect(cleared.status).toBe(200);
    expect(cleared.body).toMatchObject({ cover_offset_pct: null, cover_width_pct: 100, cover_height_pct: 12 });
    const row = (
      await pool.query(`SELECT cover_offset_pct, cover_width_pct, cover_height_pct FROM collections WHERE id = $1`, [id])
    ).rows[0];
    expect(row).toEqual({ cover_offset_pct: null, cover_width_pct: 100, cover_height_pct: 12 });
  });

  it.each([
    {},
    { name: null },
    { save_subdir: 'a/../b' },
    { kind: 'manual' },
    { cover_offset_pct: -51 },
    { cover_width_pct: 101 },
    { cover_height_pct: 51 },
    { cover_height_pct: '12' },
  ])('rejects %j with 400', async (body) => {
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

describe('GET /api/collections/:id/cover-preview', () => {
  let a: string;
  beforeEach(async () => {
    a = await channelCollection(pool, 'chA');
  });

  const REV = 'a'.repeat(32);
  const OLD_REV = 'b'.repeat(32);

  /** Puts a review project in the store, its HEAD pointing at one revision manifest, as the worker writes it. */
  function project(
    awemeId: string,
    generation: number,
    revision: string,
    manifest: { source_region?: object | null; source_cues?: number[][]; duration_ms?: number } = {},
  ): void {
    const dir = `review/${awemeId}/g${generation}/`;
    const raw = Buffer.from(
      JSON.stringify({
        schema_version: 1,
        aweme_id: awemeId,
        generation,
        revision_id: revision,
        duration_ms: manifest.duration_ms ?? 30_000,
        width: 720,
        height: 1280,
        fps: 30,
        source_region: manifest.source_region === undefined ? { x: 10, y: 1000, w: 700, h: 120 } : manifest.source_region,
        source_cues: manifest.source_cues ?? [
          [1000, 3000],
          [4000, 5000],
        ],
        cues: [],
      }),
    );
    const key = `${dir}r${revision}/manifest.json`;
    objects.set(key, raw);
    objects.set(
      `${dir}head.json`,
      Buffer.from(
        JSON.stringify({
          aweme_id: awemeId,
          generation,
          revision_id: revision,
          manifest_key: key,
          manifest_sha256: createHash('sha256').update(raw).digest('hex'),
          source: {},
        }),
      ),
    );
  }

  const sourceVideo = (awemeId: string, collectionId: string, created: string) =>
    pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, storage_path, collection_id)
       VALUES ($1, 'chA', $2::timestamptz, 'done', $3, $4)`,
      [awemeId, created, `s3://${BUCKET}/source/chA/${awemeId}.mp4`, collectionId],
    );
  const run = (awemeId: string, generation: number, status: string, withProject = true) =>
    pool.query(
      `INSERT INTO localization.runs (aweme_id, generation, workflow_id, status, project_key)
       VALUES ($1, $2, $3, $4, $5)`,
      [awemeId, generation, `video-${awemeId}-g${generation}`, status, withProject ? `review/${awemeId}/g${generation}/head.json` : null],
    );

  it('reads the HEAD manifest of the latest awaiting video and signs its source', async () => {
    await sourceVideo('100', a, '2026-11-01T00:00:00Z');
    await sourceVideo('101', a, '2026-11-10T00:00:00Z');
    await sourceVideo('102', a, '2026-11-12T00:00:00Z');
    await run('100', 1, 'ready_for_review');
    await run('100', 2, 'needs_human');
    await run('101', 1, 'approved');
    await run('102', 1, 'needs_human', false);
    project('100', 1, OLD_REV, { source_region: null });
    project('100', 2, REV);
    project('101', 1, REV);

    const res = await send('get', `/api/collections/${a}/cover-preview`);
    expect(res.status).toBe(200);
    expect(res.body).toEqual({
      aweme_id: '100',
      generation: 2,
      width: 720,
      height: 1280,
      source_region: { x: 10, y: 1000, w: 700, h: 120 },
      sample_ms: 2000,
      source_url: expect.stringMatching(/^\/api\/dashboard\/media\/[^/]+$/),
    });
    const token = (res.body.source_url as string).split('/').pop();
    expect(verifyMediaToken(SIGNING_KEY, token, Date.now())).toMatchObject({ awemeId: '100', generation: 2, kind: 'source' });
  });

  it('falls back to the most recent video when none awaits review, and to a third of the video with no source cues', async () => {
    await sourceVideo('100', a, '2026-11-01T00:00:00Z');
    await sourceVideo('101', a, '2026-11-10T00:00:00Z');
    await run('100', 1, 'approved');
    await run('101', 1, 'rendered');
    project('100', 1, OLD_REV);
    project('101', 1, REV, { source_region: null, source_cues: [], duration_ms: 30_001 });

    const res = await send('get', `/api/collections/${a}/cover-preview`);
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ aweme_id: '101', generation: 1, source_region: null, sample_ms: 10_000 });
  });

  it('answers 404 with a message when no video of the collection has a review project', async () => {
    await sourceVideo('100', a, '2026-11-01T00:00:00Z');
    await run('100', 1, 'needs_human', false);
    const res = await send('get', `/api/collections/${a}/cover-preview`);
    expect(res.status).toBe(404);
    expect(res.body.message).toContain('project review');
  });

  it('answers 404 for a collection that does not exist and 400 for an id that is not a uuid', async () => {
    expect((await send('get', `/api/collections/${MISSING}/cover-preview`)).status).toBe(404);
    expect((await send('get', `/api/collections/not-a-uuid/cover-preview`)).status).toBe(400);
  });

  it('answers 404 when the project is missing from the store', async () => {
    await sourceVideo('100', a, '2026-11-01T00:00:00Z');
    await run('100', 1, 'needs_human');
    expect((await send('get', `/api/collections/${a}/cover-preview`)).status).toBe(404);
  });

  it('refuses a manifest whose checksum differs from HEAD with 502', async () => {
    await sourceVideo('100', a, '2026-11-01T00:00:00Z');
    await run('100', 1, 'needs_human');
    project('100', 1, REV);
    const key = `review/100/g1/r${REV}/manifest.json`;
    objects.set(key, Buffer.concat([objects.get(key) as Buffer, Buffer.from(' ')]));
    expect((await send('get', `/api/collections/${a}/cover-preview`)).status).toBe(502);
  });

  it('refuses a HEAD that points outside its own project with 502', async () => {
    await sourceVideo('100', a, '2026-11-01T00:00:00Z');
    await run('100', 1, 'needs_human');
    project('100', 1, REV);
    const head = JSON.parse((objects.get('review/100/g1/head.json') as Buffer).toString());
    objects.set('review/100/g1/head.json', Buffer.from(JSON.stringify({ ...head, manifest_key: `review/999/g1/r${REV}/manifest.json` })));
    expect((await send('get', `/api/collections/${a}/cover-preview`)).status).toBe(502);
  });
});
