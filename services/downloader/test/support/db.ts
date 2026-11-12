import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { Pool } from 'pg';
import { drizzle, type NodePgDatabase } from 'drizzle-orm/node-postgres';
import * as schema from '../../src/db/schema';
import { assertSafeDdlTarget } from './pg-target-guard';

/**
 * Host-reachable connection string for docker-compose's `postgres` service.
 * These are integration tests: they run from jest on the host against a real
 * Postgres, because every invariant phase 1 owns (SKIP LOCKED, lease
 * expiry against the DB clock, partial-index plans) is Postgres runtime
 * behaviour that no mock can stand in for.
 */
export const TEST_DATABASE_URL =
  process.env.TEST_DATABASE_URL ?? 'postgres://reup:reup@localhost:5432/reup';

const MIGRATIONS_DIR = resolve(__dirname, '../../migrations');
/** Applied in this order; rolled back in the reverse order. */
const MIGRATION_NAMES = [
  '260824-init-schema',
  '261026-localization-outbox',
  '261112-dashboard-intake',
];
export const FORWARD_MIGRATIONS = MIGRATION_NAMES.map((n) => resolve(MIGRATIONS_DIR, `${n}.sql`));
export const ROLLBACK_MIGRATIONS = MIGRATION_NAMES.map((n) =>
  resolve(MIGRATIONS_DIR, `${n}.down.sql`),
).reverse();

/**
 * Every db suite gets its own Postgres schema, named per jest worker AND per
 * run.
 *
 * Jest runs test files in parallel workers by default, and all of these
 * suites operate on the same three table names. Sharing one schema would
 * make a claim test in file A race a truncate in file B -- flakiness that
 * looks exactly like a real concurrency bug and would train us to ignore
 * it. Isolating by schema (rather than serialising with --runInBand) keeps
 * the suite parallel AND keeps the DDL away from `public`, so a stray run
 * can never clobber the tables the running compose stack uses.
 *
 * `process.pid` is the per-RUN half, added in phase 2 after the worker-only
 * name produced exactly the failure this comment says it prevents. Worker
 * ids restart at 1 for every jest invocation, so two overlapping runs -- two
 * terminals, a watch mode, two CI jobs against one database -- both claim
 * `test_migration_w8`, and whichever reaches `DROP SCHEMA ... CASCADE` first
 * deletes the other's tables mid-test. Measured: four overlapping runs, 22-35
 * failures each, every one in a db suite, reported as `already exists` and
 * `relation ... does not exist`. Nothing about that reads as "you ran the
 * suite twice" -- it reads as a broken claim query, which is the most
 * expensive possible wrong conclusion in this repo.
 *
 * A jest worker process is reused across files but keeps one pid, so the
 * name stays stable for a file's setup and teardown. The cost is that a
 * SIGKILLed run leaves its schemas behind; they are empty and harmless, and
 * `DROP SCHEMA IF EXISTS` cannot reap them safely because a schema belonging
 * to a live concurrent run looks identical to an abandoned one. Clean up by
 * hand if they ever pile up:
 *
 *   select 'drop schema if exists "'||nspname||'" cascade;'
 *     from pg_namespace where nspname like 'test\_%';
 */
export function testSchemaName(label: string): string {
  const worker = process.env.JEST_WORKER_ID ?? '1';
  return `test_${label}_w${worker}_${process.pid}`.toLowerCase();
}

/**
 * Pool whose every connection is pinned to `schemaName` via a startup
 * parameter. The migration SQL is deliberately written with unqualified
 * table names so it lands wherever `search_path` points -- in production
 * that is `public`, here it is the per-worker schema.
 */
export function createTestPool(
  schemaName: string,
  opts: { statementTimeoutMs?: number; max?: number } = {},
): Pool {
  return new Pool({
    connectionString: TEST_DATABASE_URL,
    options: `-c search_path=${schemaName}`,
    statement_timeout: opts.statementTimeoutMs,
    // Small by default: the concurrency suites open one dedicated pool per
    // claimer on top of this one, and several db suites run in parallel
    // workers. A generous default here is how a suite starts failing with
    // "too many clients" on CI rather than on anything it tests.
    max: opts.max ?? 4,
  });
}

export function testDb(pool: Pool): NodePgDatabase<typeof schema> {
  return drizzle(pool, { schema });
}

/** Admin pool with no search_path pin -- used only to create/drop the schema itself. */
function createAdminPool(): Pool {
  return new Pool({ connectionString: TEST_DATABASE_URL, max: 1 });
}

export async function createEmptySchema(schemaName: string): Promise<void> {
  assertSafeDdlTarget(TEST_DATABASE_URL, 'TEST_DATABASE_URL');
  const admin = createAdminPool();
  try {
    await admin.query(`DROP SCHEMA IF EXISTS "${schemaName}" CASCADE`);
    await admin.query(`CREATE SCHEMA "${schemaName}"`);
  } finally {
    await admin.end();
  }
}

export async function dropSchema(schemaName: string): Promise<void> {
  assertSafeDdlTarget(TEST_DATABASE_URL, 'TEST_DATABASE_URL');
  const admin = createAdminPool();
  try {
    await admin.query(`DROP SCHEMA IF EXISTS "${schemaName}" CASCADE`);
  } finally {
    await admin.end();
  }
}

/**
 * Runs the checked-in migration file verbatim. Not a hand-rolled `CREATE
 * TABLE` that happens to resemble it: the artifact under test is the file an
 * operator will actually run, so anything these suites prove must be proven
 * about that exact text.
 */
export async function applyForwardMigration(pool: Pool): Promise<void> {
  assertSafeDdlTarget(TEST_DATABASE_URL, 'TEST_DATABASE_URL');
  for (const file of FORWARD_MIGRATIONS) await pool.query(readFileSync(file, 'utf8'));
}

export async function applyRollbackMigration(pool: Pool): Promise<void> {
  assertSafeDdlTarget(TEST_DATABASE_URL, 'TEST_DATABASE_URL');
  for (const file of ROLLBACK_MIGRATIONS) await pool.query(readFileSync(file, 'utf8'));
}

/** Truncates the tables so each test starts from a known-empty state. */
export async function truncateAll(pool: Pool): Promise<void> {
  await pool.query(
    `TRUNCATE videos, jobs, profiles, localization_outbox, review_decisions, publications,
              studio_open_requests, dashboard_actions RESTART IDENTITY CASCADE`,
  );
}
