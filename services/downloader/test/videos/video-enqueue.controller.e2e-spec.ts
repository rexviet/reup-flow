/**
 * `POST /api/videos` through a real Nest pipeline (`ApiKeyGuard` as `APP_GUARD`),
 * a per-run Postgres schema and a `FakeParser` -- the same harness as
 * jobs.controller.e2e-spec.ts. The parser answers with real captured
 * `fetch_one_video` envelopes (test/fixtures/aweme), not hand-written ones.
 */
import { INestApplication, Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule } from '../../src/config/config.module';
import { DB } from '../../src/db/db.module';
import { localizationOutbox, profiles, videos } from '../../src/db/schema';
import { markDone } from '../../src/db/videos.repository';
import { ParserClient } from '../../src/parser/parser.client';
import { PARSER_CLIENT } from '../../src/parser/parser.module';
import { VideoEnqueueController } from '../../src/videos/video-enqueue.controller';
import { VideosService } from '../../src/videos/videos.service';
import {
  awemeFixture,
  imagePostAweme,
  videoAweme,
  IMAGE_POST_IDS,
  MIX_PAGE_CHANNEL,
  MIX_PAGE_EPISODE_1_ID,
  MIX_PAGE_MIX_ID,
  VIDEO_ID,
} from '../support/aweme-fixtures';
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
import { fixedEnv } from '../support/fixed-env';

jest.setTimeout(30_000);

const SCHEMA = testSchemaName('video_enqueue');
const API_KEY = 'video-enqueue-e2e-test-key-not-real';

let pool: Pool;
let db: ReturnType<typeof testDb>;
let fakeParser: FakeParser;
let parserClient: ParserClient;
let app: INestApplication;

@Module({
  imports: [ConfigModule.withEnv(fixedEnv({ API_KEY }))],
  controllers: [VideoEnqueueController],
  providers: [
    VideosService,
    { provide: DB, useFactory: () => db },
    { provide: PARSER_CLIENT, useFactory: () => parserClient },
    { provide: APP_GUARD, useClass: ApiKeyGuard },
  ],
})
class TestVideoEnqueueModule {}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);

  fakeParser = new FakeParser();
  await fakeParser.start();
  parserClient = new ParserClient({ baseUrl: fakeParser.baseUrl });

  const moduleRef = await Test.createTestingModule({ imports: [TestVideoEnqueueModule] }).compile();
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

const videoUrl = (id: string): string => `https://www.douyin.com/video/${id}`;

function post(body: unknown, withKey = true): request.Test {
  const req = request(app.getHttpServer()).post('/api/videos');
  return (withKey ? req.set('X-Api-Key', API_KEY) : req).send(body as object);
}

describe('POST /api/videos', () => {
  it('requires X-Api-Key', async () => {
    const res = await post({ url: videoUrl(VIDEO_ID) }, false);
    expect(res.status).toBe(401);
    expect(fakeParser.requests).toHaveLength(0);
  });

  it.each([
    ['no url', {}],
    ['a host that is not douyin', { url: `https://evil.example/video/${VIDEO_ID}` }],
    ['a douyin page that is not a video', { url: 'https://www.douyin.com/user/MS4wLjABAAAA' }],
    ['a short link', { url: 'https://v.douyin.com/iAbCdEf/' }],
  ])('rejects %s with 400 and never calls the parser', async (_label, body) => {
    const res = await post(body);
    expect(res.status).toBe(400);
    expect(fakeParser.requests).toHaveLength(0);
  });

  it('queues the video as pending under its author and answers 202', async () => {
    fakeParser.respondWithEnvelope({ aweme_detail: videoAweme() });

    const res = await post({ url: videoUrl(VIDEO_ID) });

    expect(res.status).toBe(202);
    expect(res.body).toEqual({ aweme_id: VIDEO_ID, status: 'pending' });
    expect(fakeParser.requests).toHaveLength(1);
    expect(fakeParser.requests[0].path).toBe('/api/douyin/web/fetch_one_video');

    const [row] = await db.select().from(videos).where(eq(videos.awemeId, VIDEO_ID));
    expect(row.status).toBe('pending');
    expect(row.playAddr).toMatch(/^https?:\/\//);
    expect(row.createTime).toEqual(new Date(1741852446 * 1000));
    const [profile] = await db.select().from(profiles).where(eq(profiles.secUserId, row.secUserId));
    expect(profile).toBeDefined();
  });

  it('also reads the id from modal_id on a feed page', async () => {
    fakeParser.respondWithEnvelope({ aweme_detail: videoAweme() });
    const res = await post({ url: `https://www.douyin.com/jingxuan?modal_id=${VIDEO_ID}` });
    expect(res.status).toBe(202);
    expect(res.body.aweme_id).toBe(VIDEO_ID);
  });

  it('does not reset a video that is already downloaded', async () => {
    fakeParser.respondWithEnvelope({ aweme_detail: videoAweme() });
    await post({ url: videoUrl(VIDEO_ID) });
    await db.update(videos).set({ status: 'done', storagePath: '/data/x.mp4' }).where(eq(videos.awemeId, VIDEO_ID));

    const res = await post({ url: videoUrl(VIDEO_ID) });

    expect(res.status).toBe(202);
    expect(res.body).toEqual({ aweme_id: VIDEO_ID, status: 'done' });
    const [row] = await db.select().from(videos).where(eq(videos.awemeId, VIDEO_ID));
    expect(row.status).toBe('done');
    expect(row.storagePath).toBe('/data/x.mp4');
  });

  it('records the per-video localize override, and omitted leaves it following the channel', async () => {
    fakeParser.respondWithEnvelope({ aweme_detail: videoAweme() });
    await post({ url: videoUrl(VIDEO_ID) });
    const override = async (): Promise<boolean | null> =>
      (await db.select().from(videos).where(eq(videos.awemeId, VIDEO_ID)))[0]!.localizeOnDone;
    expect(await override()).toBeNull();

    await post({ url: videoUrl(VIDEO_ID), localize: true });
    expect(await override()).toBe(true);
    await post({ url: videoUrl(VIDEO_ID) });
    expect(await override()).toBe(true);
    await post({ url: videoUrl(VIDEO_ID), localize: false });
    expect(await override()).toBe(false);
  });

  it('localize:true on a downloaded, held video moves it to pending through the shared path', async () => {
    fakeParser.respondWithEnvelope({ aweme_detail: videoAweme() });
    await post({ url: videoUrl(VIDEO_ID) });
    await markDone(db, VIDEO_ID, 's3://bucket/x.mp4');
    const state = async (): Promise<string> =>
      (await db.select().from(localizationOutbox).where(eq(localizationOutbox.awemeId, VIDEO_ID)))[0]!.state;
    expect(await state()).toBe('held');

    await post({ url: videoUrl(VIDEO_ID) });
    expect(await state()).toBe('held');
    const res = await post({ url: videoUrl(VIDEO_ID), localize: true });
    expect(res.status).toBe(202);
    expect(await state()).toBe('pending');
    const { rows } = await pool.query('SELECT action FROM dashboard_actions');
    expect(rows).toEqual([{ action: 'localize' }]);
  });

  describe('collection', () => {
    interface Placed {
      kind: string;
      external_id: string | null;
      name: string;
      mix_episode: number | null;
      collection_manual: boolean;
    }
    async function placed(awemeId: string): Promise<Placed> {
      const { rows } = await pool.query<Placed>(
        `SELECT c.kind, c.external_id, c.name, v.mix_episode, v.collection_manual
           FROM videos v JOIN collections c ON c.id = v.collection_id WHERE v.aweme_id = $1`,
        [awemeId],
      );
      return rows[0]!;
    }

    it('puts a video with mix_info in its 合集 with its episode', async () => {
      fakeParser.respondWithEnvelope({ aweme_detail: awemeFixture('video-detail-mix.json') });

      const res = await post({ url: videoUrl(MIX_PAGE_EPISODE_1_ID) });

      expect(res.status).toBe(202);
      expect(await placed(MIX_PAGE_EPISODE_1_ID)).toEqual({
        kind: 'mix',
        external_id: MIX_PAGE_MIX_ID,
        name: '成名的代价',
        mix_episode: 1,
        collection_manual: false,
      });
      const { rows } = await pool.query(
        `SELECT name FROM collections WHERE kind = 'channel' AND external_id = $1`,
        [MIX_PAGE_CHANNEL],
      );
      expect(rows).toEqual([{ name: 'IM-薛珍珠' }]);
    });

    it('puts a video without mix_info in its channel collection', async () => {
      fakeParser.respondWithEnvelope({ aweme_detail: videoAweme() });

      await post({ url: videoUrl(VIDEO_ID) });

      expect(await placed(VIDEO_ID)).toMatchObject({ kind: 'channel', mix_episode: null, collection_manual: false });
    });

    it('puts the video in a hand-picked collection, marked manual', async () => {
      const { rows } = await pool.query<{ id: string }>(
        `INSERT INTO collections (kind, name) VALUES ('manual', 'Chọn tay') RETURNING id`,
      );
      fakeParser.respondWithEnvelope({ aweme_detail: awemeFixture('video-detail-mix.json') });

      const res = await post({ url: videoUrl(MIX_PAGE_EPISODE_1_ID), collection_id: rows[0]!.id });

      expect(res.status).toBe(202);
      expect(await placed(MIX_PAGE_EPISODE_1_ID)).toMatchObject({
        kind: 'manual',
        name: 'Chọn tay',
        collection_manual: true,
      });
    });

    it('moves a video already stored into the collection picked for it, marked manual', async () => {
      fakeParser.respondWithEnvelope({ aweme_detail: awemeFixture('video-detail-mix.json') });
      await post({ url: videoUrl(MIX_PAGE_EPISODE_1_ID) });
      const { rows } = await pool.query<{ id: string }>(
        `INSERT INTO collections (kind, name) VALUES ('manual', 'Chọn tay') RETURNING id`,
      );

      const res = await post({ url: videoUrl(MIX_PAGE_EPISODE_1_ID), collection_id: rows[0]!.id });

      expect(res.status).toBe(202);
      expect(await placed(MIX_PAGE_EPISODE_1_ID)).toMatchObject({
        kind: 'manual',
        name: 'Chọn tay',
        mix_episode: 1,
        collection_manual: true,
      });
    });

    it('leaves a stored video where it is when no collection is picked', async () => {
      fakeParser.respondWithEnvelope({ aweme_detail: awemeFixture('video-detail-mix.json') });
      await post({ url: videoUrl(MIX_PAGE_EPISODE_1_ID) });
      await pool.query(
        `UPDATE videos SET collection_manual = true, collection_id =
           (SELECT id FROM collections WHERE kind = 'channel') WHERE aweme_id = $1`,
        [MIX_PAGE_EPISODE_1_ID],
      );

      await post({ url: videoUrl(MIX_PAGE_EPISODE_1_ID) });

      expect(await placed(MIX_PAGE_EPISODE_1_ID)).toMatchObject({ kind: 'channel', collection_manual: true });
    });

    it('answers 400 for a collection_id that names no collection, before asking the parser', async () => {
      const res = await post({
        url: videoUrl(VIDEO_ID),
        collection_id: '00000000-0000-4000-8000-000000000000',
      });

      expect(res.status).toBe(400);
      expect(fakeParser.requests).toHaveLength(0);
      expect(await db.select().from(videos)).toHaveLength(0);
    });

    it('rejects a collection_id that is not a uuid with 400', async () => {
      const res = await post({ url: videoUrl(VIDEO_ID), collection_id: 'nope' });
      expect(res.status).toBe(400);
      expect(fakeParser.requests).toHaveLength(0);
    });
  });

  it('rejects a non-boolean localize with 400 and never calls the parser', async () => {
    const res = await post({ url: videoUrl(VIDEO_ID), localize: 'now' });
    expect(res.status).toBe(400);
    expect(fakeParser.requests).toHaveLength(0);
  });

  it('reports an image post as skipped instead of pretending it will download', async () => {
    fakeParser.respondWithEnvelope({ aweme_detail: imagePostAweme(0) });
    const res = await post({ url: videoUrl(IMAGE_POST_IDS[0]) });
    expect(res.status).toBe(202);
    expect(res.body.status).toBe('skipped');
  });

  it('refuses a record whose id is not the one that was asked for, and stores nothing', async () => {
    fakeParser.respondWithEnvelope({ aweme_detail: videoAweme() });
    const res = await post({ url: videoUrl('1111111111111111111') });
    expect(res.status).toBe(502);
    expect(await db.select().from(videos)).toHaveLength(0);
  });

  it('answers 404 when the parser has no such video', async () => {
    fakeParser.respondWithEnvelope({ aweme_detail: null });
    const res = await post({ url: videoUrl(VIDEO_ID) });
    expect(res.status).toBe(404);
    expect(await db.select().from(videos)).toHaveLength(0);
  });

  it('answers 502 when the parser refuses', async () => {
    fakeParser.respondWithParserError(400);
    const res = await post({ url: videoUrl(VIDEO_ID) });
    expect(res.status).toBe(502);
  });

  it('answers 503 without leaking the parser address when it is unreachable', async () => {
    fakeParser.respondWith((_req, res) => {
      res.writeHead(503);
      res.end();
    });
    const res = await post({ url: videoUrl(VIDEO_ID) });
    expect(res.status).toBe(503);
    expect(JSON.stringify(res.body)).not.toContain('127.0.0.1');
  });
});
