import { Pool } from 'pg';
import { drizzle, type NodePgDatabase } from 'drizzle-orm/node-postgres';
import * as schema from './schema';

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

export function createPool(opts: { statementTimeoutMs?: number; max?: number } = {}): Pool {
  return new Pool({
    connectionString: SPIKE_DATABASE_URL,
    statement_timeout: opts.statementTimeoutMs,
    max: opts.max ?? 10,
  });
}

export function db(pool: Pool): NodePgDatabase<typeof schema> {
  return drizzle(pool, { schema });
}

/**
 * Refuses to run this spike's DDL (DROP/CREATE TABLE) unless the connection
 * string plainly targets a local test/spike database. `spike_items` is the
 * only table this file ever drops, but `SPIKE_DATABASE_URL` is fully
 * operator-controlled and defaults to the same `reup` database phase 1's
 * real tables can live in -- an errant env var pointing this at a shared
 * staging/production host must fail loudly here, not silently DROP TABLE
 * against infrastructure this spike doesn't own.
 */
function assertSafeSpikeTarget(connectionString: string): void {
  let parsed: URL;
  try {
    parsed = new URL(connectionString);
  } catch (err) {
    throw new Error(
      `refusing to run spike DDL: SPIKE_DATABASE_URL is not a valid URL (${String(err)})`,
    );
  }
  const host = parsed.hostname;
  const database = parsed.pathname.replace(/^\//, '');
  const isLocalHost = ['localhost', '127.0.0.1', '::1'].includes(host);
  const looksLikeSpikeDb = database === 'reup' || /test|spike/i.test(database);
  if (!isLocalHost || !looksLikeSpikeDb) {
    throw new Error(
      `refusing to run spike DDL against database "${database}" on host "${host}". ` +
        'This spike only ever runs DROP/CREATE TABLE against a local test/spike ' +
        'database (host localhost/127.0.0.1/::1, db name "reup" or containing ' +
        '"test"/"spike"). Point SPIKE_DATABASE_URL at your local docker-compose ' +
        'postgres, not a shared or remote environment.',
    );
  }
}

export async function createSpikeTable(pool: Pool): Promise<void> {
  assertSafeSpikeTarget(SPIKE_DATABASE_URL);
  await pool.query('DROP TABLE IF EXISTS spike_items');
  await pool.query('CREATE TABLE spike_items (id serial PRIMARY KEY, status text NOT NULL)');
}

export async function dropSpikeTable(pool: Pool): Promise<void> {
  assertSafeSpikeTarget(SPIKE_DATABASE_URL);
  await pool.query('DROP TABLE IF EXISTS spike_items');
}
