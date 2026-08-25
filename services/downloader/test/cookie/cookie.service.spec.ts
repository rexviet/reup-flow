import type Redis from 'ioredis';
import { redactor } from '../../src/redaction/redactor';
import {
  COOKIE_TTL_SECONDS,
  CookieService,
  cookieCurrentKey,
  cookieLastOkAtKey,
  cookiePushedAtKey,
} from '../../src/cookie/cookie.service';
import { UnsupportedPlatformError } from '../../src/cookie/cookie.errors';
import { createTestRedisClient } from './redis-test-client';

/**
 * Integration suite: exercises `CookieService` against the real
 * docker-compose Redis (prefixed, see redis-test-client.ts), not a mock.
 * The behaviour under test -- a MULTI actually being atomic, a TTL actually
 * being set -- is Redis's own runtime behaviour, which a mock client cannot
 * stand in for without reimplementing it.
 */
describe('CookieService', () => {
  let redis: Redis;
  let service: CookieService;

  beforeAll(() => {
    redis = createTestRedisClient();
    service = new CookieService(redis);
  });

  afterAll(async () => {
    redis.disconnect();
  });

  beforeEach(async () => {
    await redis.del(
      cookieCurrentKey('douyin'),
      cookiePushedAtKey('douyin'),
      cookieLastOkAtKey('douyin'),
    );
    redactor.clear();
  });

  it('push writes both `current` and `pushed_at`, both with a ~24h TTL', async () => {
    await service.push('douyin', 'sentinel-fake-cookie-value-0123456789');

    const currentTtl = await redis.ttl(cookieCurrentKey('douyin'));
    const pushedAtTtl = await redis.ttl(cookiePushedAtKey('douyin'));
    const pushedAtValue = await redis.get(cookiePushedAtKey('douyin'));

    // TTL(-2) means "key does not exist" -- if `push` had only written
    // `current`, this would be the assertion that catches it.
    expect(currentTtl).toBeGreaterThan(COOKIE_TTL_SECONDS - 60);
    expect(currentTtl).toBeLessThanOrEqual(COOKIE_TTL_SECONDS);
    expect(pushedAtTtl).toBeGreaterThan(COOKIE_TTL_SECONDS - 60);
    expect(pushedAtTtl).toBeLessThanOrEqual(COOKIE_TTL_SECONDS);
    // Epoch milliseconds, as a string -- not an ISO string, not a boolean flag.
    expect(pushedAtValue).toMatch(/^\d+$/);
  });

  /**
   * The naive version of this test -- push twice, assert `age_seconds` is
   * ~0 -- proves NOTHING: a TTL-reading implementation also reports ~0
   * there, because the second push refreshed the TTL too. Both a correct
   * and a broken implementation pass that test.
   *
   * To actually discriminate, TTL and `pushed_at` must DISAGREE: push once
   * (so `current`'s TTL is fresh, ~24h), then overwrite ONLY `pushed_at`
   * with a timestamp an hour in the past, and re-verify `current`'s TTL is
   * still fresh. A TTL-reading `status()` reports ~0 here; only a
   * `pushed_at`-reading one reports ~3600.
   */
  it('age_seconds tracks `pushed_at`, NOT TTL, when the two disagree', async () => {
    await service.push('douyin', 'sentinel-fake-cookie-value-0123456789');

    const oneHourAgoMs = Date.now() - 60 * 60 * 1000;
    await redis.set(cookiePushedAtKey('douyin'), String(oneHourAgoMs), 'EX', COOKIE_TTL_SECONDS);

    // `current`'s TTL was never touched by the line above -- confirm it is
    // still fresh, so a passing assertion below can only be explained by
    // `status()` reading `pushed_at`, not by `current` having somehow
    // expired or been rewritten.
    const currentTtl = await redis.ttl(cookieCurrentKey('douyin'));
    expect(currentTtl).toBeGreaterThan(COOKIE_TTL_SECONDS - 60);

    const status = await service.status('douyin');
    expect(status.age_seconds).not.toBeNull();
    expect(status.age_seconds).toBeGreaterThanOrEqual(3595);
    expect(status.age_seconds).toBeLessThanOrEqual(3605);
  });

  /** Companion to the test above: a push must actually rewrite `pushed_at`. */
  it('age_seconds drops back to ~0 after a fresh push follows a rewound `pushed_at`', async () => {
    await service.push('douyin', 'sentinel-fake-cookie-value-0123456789');
    const oneHourAgoMs = Date.now() - 60 * 60 * 1000;
    await redis.set(cookiePushedAtKey('douyin'), String(oneHourAgoMs), 'EX', COOKIE_TTL_SECONDS);

    await service.push('douyin', 'sentinel-fake-cookie-value-9876543210');
    const status = await service.status('douyin');

    expect(status.age_seconds).not.toBeNull();
    expect(status.age_seconds).toBeGreaterThanOrEqual(0);
    expect(status.age_seconds).toBeLessThanOrEqual(5);
  });

  it('age_seconds is null when `pushed_at` is missing but `current` still exists', async () => {
    await service.push('douyin', 'sentinel-fake-cookie-value-0123456789');
    await redis.del(cookiePushedAtKey('douyin'));

    const status = await service.status('douyin');

    expect(status.present).toBe(true);
    expect(status.age_seconds).toBeNull();
  });

  it('rejects an unsupported platform and writes nothing to Redis', async () => {
    await expect(service.push('tiktok', 'sentinel-fake-cookie-value-0123456789')).rejects.toThrow(
      UnsupportedPlatformError,
    );

    // "nothing written" -- checked against the literal key this platform
    // would have used, not just "douyin"'s key (which push never touches
    // for this call anyway).
    expect(await redis.exists('tiktok:cookie:current')).toBe(0);
    expect(await redis.exists('tiktok:cookie:pushed_at')).toBe(0);
  });

  it('registers the pushed cookie with the redactor, by exact value', async () => {
    // Deliberately NOT cookie-shaped (`field=value`) -- see redactor.spec.ts
    // and this file's header comment: the pattern mechanism can never match
    // this string, so the only way `redact()` can catch it below is via
    // `registerSecret`. If `push` stopped calling `registerSecret`, this
    // test is what goes red; the earlier tests in this file would not
    // notice.
    const OPAQUE_SECRET = 'sentinel-fake-opaque-token-with-no-cookie-shape-0123456789';

    await service.push('douyin', OPAQUE_SECRET);

    const logLine = `something happened, token was ${OPAQUE_SECRET} during request`;
    expect(redactor.redact(logLine)).not.toContain(OPAQUE_SECRET);
  });
});

/**
 * The cookie must be redactable from the INSTANT it enters this process, not
 * merely by the time a successful push returns.
 *
 * The distinction is not academic. `push` registers the value with the
 * exact-value redactor and then writes it to Redis. If those two steps are
 * the other way round, the whole body of the method is a window in which the
 * cookie is live and unredactable -- and the most likely thing to write
 * during that window is the failure path itself: ioredis errors routinely
 * embed the argument they failed on, and the caller writes that to stderr.
 *
 * So the test forces the Redis write to FAIL and asserts the value is
 * scrubbed anyway. A `push` that registers last passes every happy-path
 * assertion in this file and fails only this one.
 */
describe('CookieService.push registers the cookie before it can leak', () => {
  // Marker-carrying per .gitleaks.toml's allowlist, and deliberately shaped
  // like nothing in particular: no `field=value`, so the pattern mechanism
  // cannot scrub it and only the exact-value registry can. A cookie-shaped
  // fixture here would pass whether or not registerSecret was ever called.
  const OPAQUE_COOKIE = 'sentinel-fake-unregistered-cookie-5b91';

  afterEach(() => {
    redactorClear();
  });

  it('scrubs the cookie even when the Redis write throws', async () => {
    const exploding = {
      multi: () => ({
        set: () => ({
          set: () => ({
            exec: async () => {
              throw new Error(`WRONGTYPE writing ${OPAQUE_COOKIE}`);
            },
          }),
        }),
      }),
    } as unknown as Redis;

    const service = new CookieService(exploding);

    await expect(service.push('douyin', OPAQUE_COOKIE)).rejects.toThrow();

    // The push failed. The value must still be redactable -- including out
    // of the very error the failure produced.
    expect(redactor.redact(`stderr: ${OPAQUE_COOKIE}`)).not.toContain(OPAQUE_COOKIE);
    expect(redactor.redact(`WRONGTYPE writing ${OPAQUE_COOKIE}`)).not.toContain(OPAQUE_COOKIE);
  });
});

/** Keeps the process-wide redactor from carrying this file's fixtures into
 * other suites in the same worker. */
function redactorClear(): void {
  redactor.clear();
}
