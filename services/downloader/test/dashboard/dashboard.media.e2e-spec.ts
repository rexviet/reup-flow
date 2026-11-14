/**
 * The media routes through a real Nest pipeline with the real `ApiKeyGuard`, the real
 * resolver and a scratch Postgres holding the downloader and localization schemas. Only
 * the S3 read is replaced, by an in-memory store that honors `Range`.
 */
import { INestApplication, Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import type { Pool } from 'pg';
import { Readable } from 'node:stream';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule, ENV } from '../../src/config/config.module';
import type { Env } from '../../src/config/env';
import {
  DashboardMediaService,
  MEDIA_READER,
  RangeNotSatisfiableError,
  type MediaReader,
} from '../../src/dashboard/dashboard.media';
import { DashboardMediaController } from '../../src/dashboard/dashboard.media.controller';
import { signMediaToken } from '../../src/dashboard/media-token';
import { PG_POOL } from '../../src/db/db.module';
import { createScratchDatabase, type ScratchDatabase } from '../support/localization-db';
import { channelCollection } from '../support/collections';
import { fixedEnv } from '../support/fixed-env';

jest.setTimeout(60_000);

const API_KEY = 'media-e2e-api-key-not-real';
const SIGNING_KEY = 'm'.repeat(40);
const BUCKET = 'reup-media';
const VIDEO = Buffer.from(Array.from({ length: 1000 }, (_, i) => i % 256));

class MemoryReader implements MediaReader {
  readonly opened: string[] = [];
  constructor(private readonly objects: Map<string, Buffer>) {}
  async open(key: string, range?: string) {
    this.opened.push(key);
    const data = this.objects.get(key);
    if (!data) return null;
    if (!range) {
      return { status: 200 as const, body: Readable.from([data]), contentLength: data.length, contentType: 'video/mp4' };
    }
    const m = /^bytes=(\d*)-(\d*)$/.exec(range) as RegExpExecArray;
    const start = m[1] === '' ? data.length - Number(m[2]) : Number(m[1]);
    const end = m[1] !== '' && m[2] !== '' ? Math.min(Number(m[2]), data.length - 1) : data.length - 1;
    if (start >= data.length) throw new RangeNotSatisfiableError();
    const slice = data.subarray(start, end + 1);
    return {
      status: 206 as const,
      body: Readable.from([slice]),
      contentLength: slice.length,
      contentRange: `bytes ${start}-${end}/${data.length}`,
      contentType: 'video/mp4',
    };
  }
}

const collect = (r: Readable, cb: (err: Error | null, body: Buffer) => void): void => {
  const chunks: Buffer[] = [];
  r.on('data', (c: Buffer) => chunks.push(c));
  r.on('end', () => cb(null, Buffer.concat(chunks)));
};

let scratch: ScratchDatabase;
let pool: Pool;
let app: INestApplication;
let reader: MemoryReader;

async function boot(env: Partial<Env>, withReader: boolean): Promise<INestApplication> {
  const objects = new Map<string, Buffer>([
    ['source/a/1.mp4', VIDEO],
    ['output/review/100/g3/final.mp4', VIDEO],
    ['output/review/100/g3/final.srt', Buffer.from('1\n00:00:00,000 --> 00:00:01,000\nxin chao\n')],
    ['output/review/100/g3/final.jpg', Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00])],
    ['output/100.mp4', VIDEO],
  ]);
  reader = new MemoryReader(objects);
  const fullEnv = fixedEnv({ API_KEY, DASHBOARD_MEDIA_SECRET: SIGNING_KEY, STORAGE_BACKEND: 's3', ...env });
  @Module({
    imports: [ConfigModule.withEnv(fullEnv)],
    controllers: [DashboardMediaController],
    providers: [
      { provide: PG_POOL, useValue: pool },
      { provide: MEDIA_READER, useValue: withReader ? reader : null },
      {
        provide: DashboardMediaService,
        inject: [PG_POOL, ENV, MEDIA_READER],
        useFactory: (p: Pool, e: Env, r: MediaReader | null) => new DashboardMediaService(p, e, r, Date.now),
      },
      { provide: APP_GUARD, useClass: ApiKeyGuard },
    ],
  })
  class TestMediaModule {}
  const moduleRef = await Test.createTestingModule({ imports: [TestMediaModule] }).compile();
  const created = moduleRef.createNestApplication();
  await created.init();
  return created;
}

beforeAll(async () => {
  scratch = await createScratchDatabase();
  pool = scratch.pool;
});
afterAll(() => scratch.drop());

beforeEach(async () => {
  await pool.query(
    `TRUNCATE videos, profiles, localization_outbox, localization.runs, localization.final_renders RESTART IDENTITY CASCADE`,
  );
  await pool.query(
    `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, storage_path, collection_id)
     VALUES ('100', 'ch', now(), 'done', 's3://${BUCKET}/source/a/1.mp4', $1),
            ('101', 'ch', now(), 'done', 's3://other-bucket/source/a/1.mp4', $1),
            ('102', 'ch', now(), 'done', 's3://${BUCKET}/work/private.mp4', $1),
            ('103', 'ch', now(), 'done', 's3://${BUCKET}/source/../work/private.mp4', $1)`,
    [await channelCollection(pool, 'ch')],
  );
  await pool.query(
    `INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state, final_key, srt_key)
     VALUES ('100', 3, 'rev', 'rendered', 'output/review/100/g3/final.mp4', 'output/review/100/g3/final.srt'),
            ('102', 1, 'rev', 'rendered', 'review/102-g1.mp4', 'output/review/999/g1/x.srt'),
            ('101', 1, 'rev', 'queued', 'output/review/101/g1/final.mp4', NULL)`,
  );
  app = await boot({}, true);
});
afterEach(async () => {
  await app.close();
});

const link = (body: object) =>
  request(app.getHttpServer()).post('/api/dashboard/media-links').set('X-Api-Key', API_KEY).send(body);

describe('POST /api/dashboard/media-links', () => {
  it('needs the API key', async () => {
    const res = await request(app.getHttpServer()).post('/api/dashboard/media-links').send({});
    expect(res.status).toBe(401);
  });

  it('rejects an unknown kind and a bad body with 400', async () => {
    expect((await link({ aweme_id: '100', generation: 3, kinds: ['thumbnail'] })).status).toBe(400);
    expect((await link({ aweme_id: 'abc', generation: 3, kinds: ['final'] })).status).toBe(400);
    expect((await link({ aweme_id: '100', generation: 3, kinds: [] })).status).toBe(400);
  });

  it('signs a URL for each kind that exists and marks the others unavailable', async () => {
    const res = await link({ aweme_id: '100', generation: 3, kinds: ['source', 'final', 'srt'] });
    expect(res.status).toBe(200);
    expect(res.body.links.map((l: { kind: string; available: boolean }) => [l.kind, l.available])).toEqual([
      ['source', true],
      ['final', true],
      ['srt', true],
    ]);
    expect(res.body.links[0].url).toMatch(/^\/api\/dashboard\/media\/[\w-]+\.[\w-]+$/);
    const none = await link({ aweme_id: '100', generation: 4, kinds: ['final', 'srt'] });
    expect(none.body.links).toEqual([
      { kind: 'final', available: false },
      { kind: 'srt', available: false },
    ]);
  });

  it('offers the legacy output file for a run that carries it, with a note', async () => {
    await pool.query(
      `INSERT INTO localization.runs (aweme_id, generation, workflow_id, status, output_key)
       VALUES ('100', 1, 'video-100-g1', 'published', 'output/100.mp4')`,
    );
    const res = await link({ aweme_id: '100', generation: 1, kinds: ['final'] });
    expect(res.body.links[0]).toMatchObject({ available: true, note: expect.stringContaining('không gắn generation') });
    const media = await request(app.getHttpServer()).get(res.body.links[0].url);
    expect(media.status).toBe(200);
    expect(reader.opened).toContain('output/100.mp4');
  });

  it('never offers an object outside the three accepted key shapes or another bucket', async () => {
    for (const aweme of ['101', '102', '103']) {
      const res = await link({ aweme_id: aweme, generation: 1, kinds: ['source', 'final', 'srt'] });
      expect(res.body.links.every((l: { available: boolean }) => !l.available)).toBe(true);
    }
  });
});

describe('GET /api/dashboard/media/:token', () => {
  const tokenUrl = async (kind: 'source' | 'final' | 'srt' | 'poster'): Promise<string> => {
    const res = await link({ aweme_id: '100', generation: 3, kinds: [kind] });
    return res.body.links[0].url as string;
  };

  it('streams with a valid signature and no API key', async () => {
    const res = await request(app.getHttpServer()).get(await tokenUrl('final')).buffer(true).parse(collect as never);
    expect(res.status).toBe(200);
    expect(res.headers['accept-ranges']).toBe('bytes');
    expect(res.headers['content-disposition']).toBeUndefined();
    expect((res.body as Buffer).length).toBe(1000);
  });

  it('answers 206 with exactly 100 bytes for Range: bytes=0-99', async () => {
    const res = await request(app.getHttpServer())
      .get(await tokenUrl('source'))
      .set('Range', 'bytes=0-99')
      .buffer(true)
      .parse(collect as never);
    expect(res.status).toBe(206);
    expect(res.headers['content-range']).toBe('bytes 0-99/1000');
    expect((res.body as Buffer).length).toBe(100);
  });

  it('answers 416 when the range starts past the end', async () => {
    const res = await request(app.getHttpServer()).get(await tokenUrl('source')).set('Range', 'bytes=5000-6000');
    expect(res.status).toBe(416);
  });

  it('adds Content-Disposition for ?download=1 and serves SRT as subrip', async () => {
    const res = await request(app.getHttpServer()).get(`${await tokenUrl('srt')}?download=1`);
    expect(res.status).toBe(200);
    expect(res.headers['content-disposition']).toBe('attachment; filename="100-g3.srt"');
    expect(res.headers['content-type']).toContain('application/x-subrip');
  });

  it('serves the poster, the still stored beside the video, as an image', async () => {
    const res = await request(app.getHttpServer()).get(await tokenUrl('poster')).buffer(true).parse(collect as never);
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toBe('image/jpeg');
    expect((res.body as Buffer).subarray(0, 3)).toEqual(Buffer.from([0xff, 0xd8, 0xff]));
  });

  it('offers no poster for a render whose video key is not an mp4 of the review folder', async () => {
    const res = await link({ aweme_id: '102', generation: 1, kinds: ['poster'] });
    expect(res.body.links).toEqual([{ kind: 'poster', available: false }]);
  });

  it('answers 401 without a signature, with a forged one, and when expired', async () => {
    const server = request(app.getHttpServer());
    expect((await server.get('/api/dashboard/media/nope')).status).toBe(401);
    const good = (await tokenUrl('final')).split('/').pop() as string;
    expect((await server.get(`/api/dashboard/media/${good}x`)).status).toBe(401);
    const expired = signMediaToken(SIGNING_KEY, {
      awemeId: '100',
      generation: 3,
      kind: 'final',
      expiresAtMs: Date.now() - 1000,
    });
    expect((await server.get(`/api/dashboard/media/${expired}`)).status).toBe(401);
    // A valid API key is not a substitute: the signature decides on this route.
    expect((await server.get('/api/dashboard/media/nope').set('X-Api-Key', API_KEY)).status).toBe(401);
  });

  it('answers 404 when the object is missing', async () => {
    await pool.query(
      `UPDATE localization.final_renders SET final_key = 'output/review/100/g3/missing.mp4' WHERE aweme_id = '100'`,
    );
    const token = signMediaToken(SIGNING_KEY, {
      awemeId: '100',
      generation: 3,
      kind: 'final',
      expiresAtMs: Date.now() + 60_000,
    });
    expect((await request(app.getHttpServer()).get(`/api/dashboard/media/${token}`)).status).toBe(404);
  });

  it('answers 404 when the key row is gone after the link was issued', async () => {
    const url = await tokenUrl('final');
    await pool.query(`DELETE FROM localization.final_renders WHERE aweme_id = '100'`);
    expect((await request(app.getHttpServer()).get(url)).status).toBe(404);
  });
});

describe('without S3 or a signing key', () => {
  it('answers 503 for media-links when the backend is not s3', async () => {
    await app.close();
    app = await boot({ STORAGE_BACKEND: 'local' }, false);
    const res = await link({ aweme_id: '100', generation: 3, kinds: ['final'] });
    expect(res.status).toBe(503);
    expect(JSON.stringify(res.body)).toContain('STORAGE_BACKEND=s3');
  });

  it('answers 503 when no signing key is set and 401 on the media route', async () => {
    await app.close();
    app = await boot({ DASHBOARD_MEDIA_SECRET: undefined }, true);
    expect((await link({ aweme_id: '100', generation: 3, kinds: ['final'] })).status).toBe(503);
    expect((await request(app.getHttpServer()).get('/api/dashboard/media/anything')).status).toBe(401);
  });
});
