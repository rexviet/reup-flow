import { INestApplication, Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule } from '../../src/config/config.module';
import { DashboardActionsService } from '../../src/dashboard/dashboard.actions';
import { DashboardActionsController } from '../../src/dashboard/dashboard.actions.controller';
import { fixedEnv } from '../support/fixed-env';

const API_KEY = 'actions-http-e2e-key-not-real';
const run = jest.fn(async () => ({ done: ['1'], skipped: [] }));

@Module({
  imports: [ConfigModule.withEnv(fixedEnv({ API_KEY }))],
  controllers: [DashboardActionsController],
  providers: [{ provide: DashboardActionsService, useValue: { run } }, { provide: APP_GUARD, useClass: ApiKeyGuard }],
})
class TestActionsModule {}

describe('POST /api/dashboard/actions', () => {
  let app: INestApplication;
  beforeAll(async () => {
    const moduleRef = await Test.createTestingModule({ imports: [TestActionsModule] }).compile();
    app = moduleRef.createNestApplication();
    await app.init();
  });
  afterAll(() => app.close());
  beforeEach(() => run.mockClear());

  const post = (payload: unknown, key: string | null = API_KEY) => {
    const req = request(app.getHttpServer()).post('/api/dashboard/actions');
    return (key ? req.set('X-Api-Key', key) : req).send(payload as object);
  };

  it('needs the API key', async () => {
    expect((await post({ action: 'localize', aweme_ids: ['1'] }, null)).status).toBe(401);
    expect(run).not.toHaveBeenCalled();
  });

  it('passes a valid body to the service and returns its result', async () => {
    const res = await post({ action: 'localize', aweme_ids: ['1'] });
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ done: ['1'], skipped: [] });
    expect(run).toHaveBeenCalledTimes(1);
  });

  it('accepts exactly 100 ids and rejects 101 with 400', async () => {
    const ids = (n: number) => Array.from({ length: n }, (_, i) => String(i + 1));
    expect((await post({ action: 'localize', aweme_ids: ids(100) })).status).toBe(200);
    expect((await post({ action: 'localize', aweme_ids: ids(101) })).status).toBe(400);
  });

  it.each([
    { action: 'localize', aweme_ids: [] },
    { action: 'explode', aweme_ids: ['1'] },
    { action: 'localize', aweme_ids: ['1'], extra: true },
    { action: 'approve', aweme_ids: ['1'] },
    { action: 'mark_published', aweme_ids: ['1'], post_url: 'javascript:alert(1)' },
  ])('rejects %j with 400 before the service runs', async (payload) => {
    expect((await post(payload)).status).toBe(400);
    expect(run).not.toHaveBeenCalled();
  });
});
