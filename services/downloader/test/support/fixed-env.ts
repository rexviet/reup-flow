import type { Env } from '../../src/config/env';

/**
 * A complete, valid `Env` for tests that need to inject configuration via
 * `ConfigModule.withEnv()` instead of parsing `process.env`. Points at the
 * live docker-compose stack (localhost:5432 / :6379) by default so module
 * tests that actually connect (DbModule, RedisModule) work against it
 * out of the box; override individual fields to test failure paths.
 */
export function fixedEnv(overrides: Partial<Env> = {}): Env {
  return {
    API_KEY: 'test-key-not-real',
    DATABASE_URL: 'postgres://reup:reup@localhost:5432/reup',
    REDIS_URL: 'redis://localhost:6379',
    PARSER_BASE_URL: 'http://parser:80',
    PROBE_SEC_USER_ID: 'probe-sec-user-id',
    DATA_DIR: '/data',
    DOWNLOAD_CONCURRENCY: 6,
    ENUMERATE_SLEEP_MS_MIN: 3000,
    ENUMERATE_SLEEP_MS_MAX: 5000,
    LEASE_SECONDS: 300,
    MAX_ATTEMPTS: 3,
    URL_TTL_SECONDS: 7200,
    DISK_RESERVE_BYTES: 10737418240,
    EST_BYTES_PER_VIDEO: 31457280,
    PORT: 3000,
    // CORS off by default, matching `configureApp`'s own default: these are
    // in-process module tests with no browser and no Origin header, so an
    // allowlist here would configure a mechanism nothing in them exercises.
    // A test that wants CORS says so.
    CORS_ALLOWED_ORIGINS: [],
    // Lanes OFF by default in tests. A test that wants them says so; nothing
    // should start a 60s reaper against the live compose stack merely by
    // building an Env.
    LANES_ENABLED: false,
    // Local disk, the default. A test of the s3 backend overrides the whole
    // group; none of these values reach a network in the meantime.
    STORAGE_BACKEND: 'local',
    S3_REGION: 'us-east-1',
    S3_BUCKET: 'reup-media',
    S3_FORCE_PATH_STYLE: true,
    S3_KEY_PREFIX: 'source/',
    S3_STALE_UPLOAD_MS: 86_400_000,
    ...overrides,
  };
}
