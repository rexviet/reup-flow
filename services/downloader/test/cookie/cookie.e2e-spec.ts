import { INestApplication } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ConfigModule } from '../../src/config/config.module';
import { CookieModule } from '../../src/cookie/cookie.module';
import { cookieCurrentKey, cookiePushedAtKey } from '../../src/cookie/cookie.service';
import { RedisModule, REDIS } from '../../src/redis/redis.module';
import { redactor } from '../../src/redaction/redactor';
import { fixedEnv } from '../support/fixed-env';
import { createTestRedisClient } from './redis-test-client';

const API_KEY = 'e2e-cookie-test-key-not-real';

/**
 * Builds its own testing module rather than importing `AppModule` (owned by
 * another concurrent phase, wiring `CookieModule` in afterwards) --
 * `test/auth/api-key.guard.e2e-spec.ts` is the template this follows: the
 * guard is registered directly as `APP_GUARD`, exactly as `AppModule` will
 * do, so this exercises the real HTTP pipeline shape without depending on
 * `AppModule` existing yet.
 */
describe('Cookie endpoints (e2e)', () => {
  let app: INestApplication;
  let testRedis: ReturnType<typeof createTestRedisClient>;

  beforeAll(async () => {
    testRedis = createTestRedisClient();

    const moduleRef = await Test.createTestingModule({
      imports: [ConfigModule.withEnv(fixedEnv({ API_KEY })), RedisModule, CookieModule],
      providers: [{ provide: APP_GUARD, useClass: ApiKeyGuard }],
    })
      // Swaps RedisModule's own factory-created client (unprefixed -- it
      // would write the LITERAL `douyin:cookie:current` on the shared
      // compose Redis the running parser polls) for one scoped to this test
      // run. See redis-test-client.ts for why worker id alone isn't enough.
      .overrideProvider(REDIS)
      .useValue(testRedis)
      .compile();

    app = moduleRef.createNestApplication();
    await app.init();
  });

  afterAll(async () => {
    await testRedis.del(cookieCurrentKey('douyin'), cookiePushedAtKey('douyin'));
    testRedis.disconnect();
    await app.close();
  });

  beforeEach(() => {
    redactor.clear();
  });

  it('returns 401 on POST /internal/cookie with no X-Api-Key', async () => {
    const res = await request(app.getHttpServer())
      .post('/internal/cookie')
      .send({ platform: 'douyin', cookie: 'sentinel-fake-unauthenticated-0123456789' });

    expect(res.status).toBe(401);
  });

  it('returns 401 on GET /internal/cookie/status with no X-Api-Key', async () => {
    const res = await request(app.getHttpServer()).get('/internal/cookie/status');
    expect(res.status).toBe(401);
  });

  it('returns 401 on both routes with a wrong X-Api-Key', async () => {
    const push = await request(app.getHttpServer())
      .post('/internal/cookie')
      .set('X-Api-Key', 'wrong-key-should-not-appear-in-response')
      .send({ platform: 'douyin', cookie: 'sentinel-fake-wrong-key-0123456789' });
    const status = await request(app.getHttpServer())
      .get('/internal/cookie/status')
      .set('X-Api-Key', 'wrong-key-should-not-appear-in-response');

    expect(push.status).toBe(401);
    expect(status.status).toBe(401);
  });

  it('returns 400 for an unsupported platform, without writing anything to Redis', async () => {
    const res = await request(app.getHttpServer())
      .post('/internal/cookie')
      .set('X-Api-Key', API_KEY)
      .send({ platform: 'tiktok', cookie: 'sentinel-fake-unsupported-platform-0123456789' });

    expect(res.status).toBe(400);
    expect(await testRedis.exists('tiktok:cookie:current')).toBe(0);
    expect(await testRedis.exists('tiktok:cookie:pushed_at')).toBe(0);
  });

  // FIX 8 (review): the schema used to accept ANY non-empty cookie
  // (`.min(1)`), while `Redactor.registerSecret` silently ignores anything
  // under `MIN_REGISTERED_SECRET_LENGTH` (8) characters -- so a short cookie
  // was both trivially brute-forceable (see `src/config/env.ts`'s API_KEY
  // for the same class of problem) AND accepted by this endpoint while
  // ending up completely unprotected by the exact-value registry the moment
  // it appeared in any log line. The schema now imports the SAME constant
  // the registry enforces, so the two cannot drift apart.
  it('returns 400 for a cookie shorter than MIN_REGISTERED_SECRET_LENGTH', async () => {
    const res = await request(app.getHttpServer())
      .post('/internal/cookie')
      .set('X-Api-Key', API_KEY)
      .send({ platform: 'douyin', cookie: 'short1' }); // 6 chars, below the 8-char floor

    expect(res.status).toBe(400);
  });

  it('returns 400 for a malformed body, and the 400 body does not echo the cookie', async () => {
    const res = await request(app.getHttpServer())
      .post('/internal/cookie')
      .set('X-Api-Key', API_KEY)
      .send({ platform: 'douyin', cookie: '' });

    expect(res.status).toBe(400);
    expect(JSON.stringify(res.body)).not.toContain('sentinel');
  });

  it(
    'returns 400 when the request fails validation for an UNRELATED reason ' +
      '(missing platform), and a real-looking cookie value in the same body still never ' +
      'appears in the response',
    async () => {
      // This is the actual trap, not the empty-string case above: the body
      // carries a well-formed, distinctive cookie value, but validation
      // fails for a different field. A zod error message built from
      // `issue.received` on any path would risk echoing this string back --
      // see `formatValidationError`'s comment and `src/config/env.ts`'s
      // `parseEnv` for the same trap on `API_KEY`.
      const COOKIE = 'sentinel-fake-must-never-leak-in-a-400-0123456789';

      const res = await request(app.getHttpServer())
        .post('/internal/cookie')
        .set('X-Api-Key', API_KEY)
        .send({ cookie: COOKIE });

      expect(res.status).toBe(400);
      expect(JSON.stringify(res.body)).not.toContain(COOKIE);
    },
  );

  it(
    'pushes a cookie, then GET /internal/cookie/status never contains the cookie value ' +
      'in ANY field of the serialised response',
    async () => {
      const COOKIE = 'sentinel-fake-e2e-cookie-value-0123456789';

      const pushRes = await request(app.getHttpServer())
        .post('/internal/cookie')
        .set('X-Api-Key', API_KEY)
        .send({ platform: 'douyin', cookie: COOKIE });

      expect(pushRes.status).toBe(200);
      expect(pushRes.body).toEqual({ ok: true });
      expect(JSON.stringify(pushRes.body)).not.toContain(COOKIE);

      const statusRes = await request(app.getHttpServer())
        .get('/internal/cookie/status')
        .set('X-Api-Key', API_KEY);

      expect(statusRes.status).toBe(200);
      // Asserted against the WHOLE serialised body, not field-by-field --
      // a field-by-field check passes the moment somebody adds a new field
      // that happens to carry the cookie.
      expect(JSON.stringify(statusRes.body)).not.toContain(COOKIE);
      expect(statusRes.body.present).toBe(true);
      expect(typeof statusRes.body.age_seconds).toBe('number');
    },
  );
});
