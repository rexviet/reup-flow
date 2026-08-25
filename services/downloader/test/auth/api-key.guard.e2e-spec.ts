import { Controller, Get, INestApplication, Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule } from '../../src/config/config.module';
import { fixedEnv } from '../support/fixed-env';

/**
 * Exercises the guard the way it actually runs in production: registered
 * once as APP_GUARD, in front of a real Nest HTTP pipeline, rather than
 * called directly. `/internal/probe` stands in for the real `/internal/*`
 * routes that land in groups B/C -- the DoD requires 401 on "every route,
 * /api and /internal alike", and this proves the guard does not special-
 * case a path prefix.
 */
@Controller('api/probe')
class ApiProbeController {
  @Get()
  probe(): { ok: true } {
    return { ok: true };
  }
}

@Controller('internal/probe')
class InternalProbeController {
  @Get()
  probe(): { ok: true } {
    return { ok: true };
  }
}

const API_KEY = 'e2e-test-key-not-real';

@Module({
  imports: [ConfigModule.withEnv(fixedEnv({ API_KEY }))],
  controllers: [ApiProbeController, InternalProbeController],
  providers: [{ provide: APP_GUARD, useClass: ApiKeyGuard }],
})
class ProbeAppModule {}

describe('ApiKeyGuard (e2e, registered as APP_GUARD)', () => {
  let app: INestApplication;

  beforeAll(async () => {
    const moduleRef = await Test.createTestingModule({ imports: [ProbeAppModule] }).compile();
    app = moduleRef.createNestApplication();
    await app.init();
  });

  afterAll(async () => {
    await app.close();
  });

  it('returns 401 on /api/probe with no X-Api-Key header', async () => {
    const res = await request(app.getHttpServer()).get('/api/probe');
    expect(res.status).toBe(401);
  });

  it('returns 401 on /internal/probe with no X-Api-Key header (internal is not exempt)', async () => {
    const res = await request(app.getHttpServer()).get('/internal/probe');
    expect(res.status).toBe(401);
  });

  it('returns 401 with a wrong X-Api-Key, and the wrong key is not echoed in the body', async () => {
    const wrongKey = 'wrong-key-should-not-appear-in-response';
    const res = await request(app.getHttpServer()).get('/api/probe').set('X-Api-Key', wrongKey);

    expect(res.status).toBe(401);
    expect(JSON.stringify(res.body)).not.toContain(wrongKey);
  });

  it('returns 200 on /api/probe with the correct X-Api-Key', async () => {
    const res = await request(app.getHttpServer()).get('/api/probe').set('X-Api-Key', API_KEY);
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ ok: true });
  });

  it('returns 200 on /internal/probe with the correct X-Api-Key', async () => {
    const res = await request(app.getHttpServer())
      .get('/internal/probe')
      .set('X-Api-Key', API_KEY);
    expect(res.status).toBe(200);
  });
});
