import 'reflect-metadata';
import { type INestApplication } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule } from '../../src/config/config.module';
import { WorkspaceController } from '../../src/workspace/workspace.controller';
import { WorkspaceService } from '../../src/workspace/workspace.service';
import { snapshotSchema } from '../../src/workspace/workspace.schemas';
import { fixedEnv } from '../support/fixed-env';

describe('GET /api/workspace/snapshot', () => {
  let app: INestApplication;
  const KEY = 'workspace-test-key-0123456789';

  beforeAll(async () => {
    const temporalDown = { listOpen: async () => { throw new Error('down'); }, inspect: async () => { throw new Error('down'); }, find: async () => { throw new Error('down'); } };
    const postgresDown = async () => { throw new Error('down'); };
    const service = new WorkspaceService(
      temporalDown,
      { recentLlmCalls: postgresDown, recentRuns: postgresDown, runCosts: postgresDown, run: postgresDown, costByStage: postgresDown },
    );
    const module = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv({ API_KEY: KEY }))],
      controllers: [WorkspaceController],
      providers: [{ provide: APP_GUARD, useClass: ApiKeyGuard }, { provide: WorkspaceService, useValue: service }],
    }).compile();
    app = module.createNestApplication();
    await app.init();
  });
  afterAll(() => app.close());

  it('needs the API key like every route', async () => {
    await request(app.getHttpServer()).get('/api/workspace/snapshot').expect(401);
  });

  it('answers 200 with both sources down, and the body says so', async () => {
    const res = await request(app.getHttpServer()).get('/api/workspace/snapshot').set('x-api-key', KEY).expect(200);
    expect(snapshotSchema.parse(res.body).sources).toEqual({ temporal: 'down', postgres: 'down' });
    expect(res.body.agents.every((a: { state: string }) => a.state === 'idle')).toBe(true);
  });

  describe('GET /api/workspace/runs/:awemeId/:generation', () => {
    const get = (path: string) => request(app.getHttpServer()).get(path).set('x-api-key', KEY);

    it('rejects a bad awemeId or generation with 400', async () => {
      await get('/api/workspace/runs/not-a-number/1').expect(400);
      await get('/api/workspace/runs/123/-1').expect(400);
      await get('/api/workspace/runs/123/abc').expect(400);
    });

    it('answers 503, not 404, when the sources that would know the run are down', async () => {
      await get('/api/workspace/runs/123/1').expect(503);
    });
  });

  it('has no write route', async () => {
    for (const method of ['post', 'put', 'patch', 'delete'] as const) {
      await request(app.getHttpServer())[method]('/api/workspace/snapshot').set('x-api-key', KEY).expect(404);
    }
  });
});

describe('GET /api/workspace/runs/:awemeId/:generation when the run is unknown', () => {
  it('answers 404', async () => {
    const KEY = 'workspace-test-key-0123456789';
    const module = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv({ API_KEY: KEY }))],
      controllers: [WorkspaceController],
      providers: [
        { provide: APP_GUARD, useClass: ApiKeyGuard },
        { provide: WorkspaceService, useValue: { runDetail: async () => null } },
      ],
    }).compile();
    const app = module.createNestApplication();
    await app.init();
    await request(app.getHttpServer()).get('/api/workspace/runs/123/1').set('x-api-key', KEY).expect(404);
    await app.close();
  });
});
