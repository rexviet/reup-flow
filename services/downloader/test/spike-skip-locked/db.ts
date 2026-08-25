import { Pool } from 'pg';
import { drizzle, type NodePgDatabase } from 'drizzle-orm/node-postgres';
import * as schema from './schema';
import { assertSafeDdlTarget } from '../support/pg-target-guard';
import { createEmptySchema, dropSchema, testSchemaName } from '../support/db';

/**
 * Host-reachable connection string for docker-compose's `postgres` service
 * (exposed on 5432, see root docker-compose.yml comment). This spike runs
 * from the host (jest), not from inside a container, so it deliberately
 * does NOT read the container-internal DATABASE_URL from .env.
 */
export const SPIKE_DATABASE_URL =
  process.env.SPIKE_DATABASE_URL ?? 'postgres://reup:reup@localhost:5432/reup';

/**
 * Short enough that if SKIP LOCKED were missing and a claim blocked on
 * another transaction's held lock, the connection errors out instead of
 * hanging the suite forever. Passed as a pg connection startup parameter
 * (node-postgres issues it during connection setup) -- not a `SET`
 * statement we author ourselves.
 */
export const STATEMENT_TIMEOUT_MS = 2_000;

/**
 * Private schema for this spike, unique per jest worker AND per run.
 *
 * This file predates `test/support/db.ts` and used a bare `spike_items` in
 * `public`. Phase 2 measured what that costs: with several `pnpm test` runs
 * overlapping, one run's `TRUNCATE spike_items` blocks behind the row locks
 * another run's concurrency harness is DELIBERATELY holding open, and the
 * afterEach hook times out. The failure lands on the suite whose entire job
 * is to prove claims behave correctly under concurrent locking -- so the
 * most natural reading of the red is "SKIP LOCKED is broken", which is both
 * wrong and the single most expensive wrong conclusion available in this
 * repo.
 *
 * Same isolation the db suites get, for the same reason.
 */
export const SPIKE_SCHEMA = testSchemaName('spike');

export function createPool(opts: { statementTimeoutMs?: number; max?: number } = {}): Pool {
  return new Pool({
    connectionString: SPIKE_DATABASE_URL,
    options: `-c search_path=${SPIKE_SCHEMA}`,
    statement_timeout: opts.statementTimeoutMs,
    max: opts.max ?? 10,
  });
}

export function db(pool: Pool): NodePgDatabase<typeof schema> {
  return drizzle(pool, { schema });
}

/**
 * Phase 1 promoted this guard to test/support/pg-target-guard.ts so the
 * migration suite -- which drops the REAL tables, not just `spike_items` --
 * is protected by exactly the same check. Kept as a thin local alias so the
 * two call sites below read unchanged.
 */
function assertSafeSpikeTarget(connectionString: string): void {
  assertSafeDdlTarget(connectionString, 'SPIKE_DATABASE_URL');
}

export async function createSpikeTable(pool: Pool): Promise<void> {
  assertSafeSpikeTarget(SPIKE_DATABASE_URL);
  await createEmptySchema(SPIKE_SCHEMA);
  // Unqualified on purpose: it lands wherever the pool's search_path points,
  // which is this run's private schema and never `public`.
  await pool.query('CREATE TABLE spike_items (id serial PRIMARY KEY, status text NOT NULL)');
}

export async function dropSpikeTable(_pool: Pool): Promise<void> {
  assertSafeSpikeTarget(SPIKE_DATABASE_URL);
  await dropSchema(SPIKE_SCHEMA);
}
