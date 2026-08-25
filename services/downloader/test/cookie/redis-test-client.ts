import Redis from 'ioredis';

/**
 * Host-reachable Redis URL for docker-compose's `redis` service. Same
 * default as `test/support/fixed-env.ts`'s `REDIS_URL`, duplicated rather
 * than imported: this phase owns only `src/cookie/**` and `test/cookie/**`,
 * and reaching into `test/support/` would cross that boundary for a single
 * string constant.
 */
const TEST_REDIS_URL = process.env.TEST_REDIS_URL ?? 'redis://localhost:6379';

/**
 * A dedicated ioredis client whose `keyPrefix` is unique per (jest worker,
 * OS process). ioredis applies `keyPrefix` transparently to every command
 * that takes a key -- EXISTS, GET, SET, DEL, and every command queued inside
 * a MULTI -- so `CookieService` never has to know it is running against a
 * sandboxed slice of the shared compose Redis instead of the real thing.
 *
 * This exists because `CookieService` writes the LITERAL keys
 * `douyin:cookie:current` / `douyin:cookie:pushed_at`, and the live parser
 * fork polls `douyin:cookie:current` on every request against the SAME
 * compose Redis these tests run against. A test that wrote the unprefixed
 * key would swap the running stack's cookie out from under it mid-test.
 *
 * `JEST_WORKER_ID` alone is not enough to isolate a run: it restarts at 1
 * for every jest invocation, so two overlapping runs (two terminals, a
 * watch mode left open, two CI jobs against one Redis) both claim worker id
 * 1 and stomp each other's keys -- `test/support/db.ts`'s `testSchemaName`
 * documents the identical failure mode for Postgres schemas, measured there
 * at 22-35 failures per overlapping run, none of which read as "you ran the
 * suite twice". Mixing in `process.pid` -- stable for the lifetime of one
 * run, different across runs -- is what actually makes the prefix unique
 * per RUN rather than merely per worker slot.
 *
 * `lazyConnect: true`: `cookie.e2e-spec.ts` hands this client to
 * `RedisModule.overrideProvider`, and `RedisModule.onModuleInit` calls
 * `client.connect()` itself as its own boot probe. Without `lazyConnect`,
 * ioredis starts connecting the instant this constructor runs, so that later
 * `connect()` call rejects with "Redis is already connecting/connected" and
 * the whole test application fails to boot. ioredis still connects
 * automatically on the first command issued against it either way, so this
 * changes nothing for `cookie.service.spec.ts`, which never calls
 * `connect()` itself.
 */
export function createTestRedisClient(): Redis {
  const worker = process.env.JEST_WORKER_ID ?? '1';
  const keyPrefix = `test:cookie:w${worker}:${process.pid}:`;
  return new Redis(TEST_REDIS_URL, { keyPrefix, lazyConnect: true });
}
