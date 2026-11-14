import 'reflect-metadata';
import { type INestApplication } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule } from '../../src/config/config.module';
import { DashboardController } from '../../src/dashboard/dashboard.controller';
import { DashboardHealthService } from '../../src/dashboard/dashboard.health.service';
import { DashboardService } from '../../src/dashboard/dashboard.service';
import { fixedEnv } from '../support/fixed-env';

describe('/api/dashboard', () => {
  let app: INestApplication;
  const KEY = 'dashboard-test-key-0123456789';
  const seen: unknown[] = [];

  beforeAll(async () => {
    const downStore = new Proxy({}, { get: () => async () => { throw new Error('down'); } });
    const temporalDown = {
      listOpenLocalizationIds: async () => { throw new Error('down'); },
      workflowStatus: async () => { throw new Error('down'); },
      pendingActivities: async () => { throw new Error('down'); },
      inspect: async () => { throw new Error('down'); },
    };
    const service = new DashboardService(downStore as never, temporalDown, 30);
    const list = service.list.bind(service);
    service.list = (q) => (seen.push(q), list(q));
    const module = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv({ API_KEY: KEY }))],
      controllers: [DashboardController],
      providers: [
        { provide: APP_GUARD, useClass: ApiKeyGuard },
        { provide: DashboardService, useValue: service },
        { provide: DashboardHealthService, useValue: { report: async () => ({ postgres: { status: 'ok' } }) } },
      ],
    }).compile();
    app = module.createNestApplication();
    await app.init();
  });
  afterAll(() => app.close());

  const get = (path: string) => request(app.getHttpServer()).get(path).set('x-api-key', KEY);

  it('needs the API key on every route', async () => {
    for (const path of ['videos', 'videos/1', 'attention', 'channels', 'health']) {
      await request(app.getHttpServer()).get(`/api/dashboard/${path}`).expect(401);
    }
  });

  describe('GET /videos', () => {
    it('answers 200 with sources when postgres and Temporal are both down', async () => {
      const res = await get('/api/dashboard/videos').expect(200);
      expect(res.body).toMatchObject({ items: [], total: 0, sources: { postgres: 'down', temporal: 'down' } });
      expect(Object.keys(res.body.counts_by_stage)).toContain('awaiting_review');
    });

    it.each([
      'stage=nope',
      'stage=held;drop',
      'limit=0',
      'limit=201',
      'limit=abc',
      'offset=-1',
      'from=yesterday',
      'to=2026-13-45',
      'attention=maybe',
      'channel=',
      `q=${'x'.repeat(101)}`,
      'unknown=1',
      'collection=not-a-uuid',
      'collection=',
    ])('rejects ?%s with 400', async (qs) => {
      await get(`/api/dashboard/videos?${qs}`).expect(400);
    });

    it('parses a full valid query', async () => {
      seen.length = 0;
      await get('/api/dashboard/videos?stage=awaiting_review&channel=c1&q=50%25_&from=2026-11-01&to=2026-11-30T00:00:00Z&attention=true&limit=10&offset=5&collection=0A1B2C3D-0000-4000-8000-000000000000').expect(200);
      expect(seen[0]).toMatchObject({
        stage: 'awaiting_review',
        channel: 'c1',
        q: '50%_',
        from: '2026-11-01T00:00:00.000Z',
        to: '2026-11-30T00:00:00.000Z',
        attention: true,
        limit: 10,
        offset: 5,
        collection: '0a1b2c3d-0000-4000-8000-000000000000',
      });
    });
  });

  it('reads a date-only `to` as the end of that day', async () => {
    seen.length = 0;
    await get('/api/dashboard/videos?to=2026-11-12').expect(200);
    expect(seen[0]).toMatchObject({ to: '2026-11-12T23:59:59.999Z' });
  });

  describe('GET /videos/:awemeId', () => {
    it.each(['abc', '1a', '123456789012345678901234567890123', '%20', '1.5'])('rejects id %s with 400', async (id) => {
      await get(`/api/dashboard/videos/${id}`).expect(400);
    });

    it('answers 200 with a null video and sources when postgres is down', async () => {
      const res = await get('/api/dashboard/videos/123').expect(200);
      expect(res.body).toEqual({ video: null, sources: { postgres: 'down', temporal: 'down' } });
    });
  });

  it('answers attention and channels with 200 while sources are down', async () => {
    const attention = await get('/api/dashboard/attention').expect(200);
    expect(attention.body).toMatchObject({ jobs: [], items: [], sources: { postgres: 'down' } });
    const channels = await get('/api/dashboard/channels').expect(200);
    expect(channels.body).toMatchObject({ items: [], sources: { postgres: 'down' } });
  });

  it('serves health', async () => {
    const res = await get('/api/dashboard/health').expect(200);
    expect(res.body.postgres.status).toBe('ok');
  });

  it('has no write route', async () => {
    for (const method of ['post', 'put', 'patch', 'delete'] as const) {
      for (const path of ['videos', 'videos/1', 'attention', 'channels', 'health']) {
        await request(app.getHttpServer())[method](`/api/dashboard/${path}`).set('x-api-key', KEY).expect(404);
      }
    }
  });
});
