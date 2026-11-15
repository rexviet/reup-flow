/**
 * Phase 1 -- query-plan gate (phase-01 Risks, DoD "EXPLAIN uses the right
 * index").
 *
 * Every other test in this phase passes just as happily against a table with
 * no indexes at all: correctness does not depend on them. The failure they
 * guard against is a seq scan over 100k videos on every single claim, which
 * looks like nothing at 10 rows and like an outage at scale. So these tests
 * seed a realistic distribution, ANALYZE, and assert the planner actually
 * reaches for the partial indexes the migration creates.
 *
 * The SQL under EXPLAIN is CAPTURED FROM THE REAL REPOSITORY CALL via
 * Drizzle's query logger, never re-typed here. A hand-copied query would
 * drift from the repository and then cheerfully report on a plan nothing
 * executes.
 */
import type { Pool } from 'pg';
import { drizzle } from 'drizzle-orm/node-postgres';
import * as schema from '../../src/db/schema';
import * as videosRepo from '../../src/db/videos.repository';
import * as jobsRepo from '../../src/db/jobs.repository';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testSchemaName,
} from '../support/db';

jest.setTimeout(120_000);

const SCHEMA = testSchemaName('plan');
const OPTS = { leaseSeconds: 300, maxAttempts: 3 };

let pool: Pool;

interface Captured {
  query: string;
  params: unknown[];
}

/**
 * Runs `fn` against a Drizzle handle that records every statement it issues,
 * and returns the last one. This is the exact text and parameter list the
 * repository sends to Postgres.
 */
async function capture(fn: (db: ReturnType<typeof drizzle>) => Promise<unknown>): Promise<Captured> {
  const captured: Captured[] = [];
  const logged = drizzle(pool, {
    schema,
    logger: { logQuery: (query, params) => captured.push({ query, params }) },
  });
  await fn(logged as never);
  if (captured.length !== 1) {
    throw new Error(`expected exactly one statement, captured ${captured.length}`);
  }
  return captured[0]!;
}

/** Every "Index Name" anywhere in an EXPLAIN (FORMAT JSON) plan tree. */
function indexNames(node: unknown, found: string[] = []): string[] {
  if (Array.isArray(node)) {
    for (const child of node) indexNames(child, found);
  } else if (node && typeof node === 'object') {
    for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
      if (key === 'Index Name' && typeof value === 'string') found.push(value);
      else indexNames(value, found);
    }
  }
  return found;
}

/** Every "Node Type" that is a sequential scan, with the relation it scans. */
function seqScannedRelations(node: unknown, found: string[] = []): string[] {
  if (Array.isArray(node)) {
    for (const child of node) seqScannedRelations(child, found);
  } else if (node && typeof node === 'object') {
    const obj = node as Record<string, unknown>;
    if (obj['Node Type'] === 'Seq Scan' && typeof obj['Relation Name'] === 'string') {
      found.push(obj['Relation Name']);
    }
    for (const value of Object.values(obj)) seqScannedRelations(value, found);
  }
  return found;
}

async function explain(captured: Captured): Promise<unknown> {
  const { rows } = await pool.query<{ 'QUERY PLAN': unknown }>(
    `EXPLAIN (FORMAT JSON) ${captured.query}`,
    captured.params as unknown[],
  );
  return rows[0]!['QUERY PLAN'];
}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);

  // A realistic mid-job distribution: most work already done, a slice still
  // outstanding, a few in flight. The partial indexes only look attractive
  // to the planner when 'pending' is a small fraction of the table -- which
  // is exactly the situation they exist for.
  await pool.query(`INSERT INTO profiles (sec_user_id) SELECT 'profile-' || i FROM generate_series(0, 19) AS i`);
  await pool.query(`
    INSERT INTO collections (kind, external_id, sec_user_id, name)
    SELECT 'channel', sec_user_id, sec_user_id, sec_user_id FROM profiles
  `);
  await pool.query(`
    INSERT INTO videos (aweme_id, sec_user_id, create_time, status, lease_expires_at, collection_id)
    SELECT
      'v' || i,
      'profile-' || (i % 20),
      timestamptz '2026-01-01' + (i || ' minutes')::interval,
      CASE WHEN i % 25 = 0 THEN 'downloading' WHEN i % 5 = 0 THEN 'pending' ELSE 'done' END,
      CASE WHEN i % 25 = 0 THEN now() + interval '5 minutes' END,
      c.id
    FROM generate_series(1, 50000) AS i
    JOIN collections c ON c.kind = 'channel' AND c.external_id = 'profile-' || (i % 20)
  `);
  await pool.query(`
    INSERT INTO jobs (sec_user_id, status, created_at, lease_expires_at)
    SELECT
      'profile-' || (i % 20),
      CASE WHEN i % 25 = 0 THEN 'enumerating' WHEN i % 5 = 0 THEN 'pending' ELSE 'completed' END,
      timestamptz '2026-01-01' + (i || ' minutes')::interval,
      CASE WHEN i % 25 = 0 THEN now() + interval '5 minutes' END
    FROM generate_series(1, 50000) AS i
  `);
  await pool.query('ANALYZE videos, jobs, profiles');
});

afterAll(async () => {
  await pool.end();
  await dropSchema(SCHEMA);
});

it('the video claim uses the partial pending index, not a seq scan', async () => {
  const plan = await explain(await capture((db) => videosRepo.claimOne(db as never, OPTS)));

  expect(indexNames(plan)).toContain('videos_pending_claim_idx');
  expect(seqScannedRelations(plan)).not.toContain('videos');
});

it('the video reaper uses the partial downloading index', async () => {
  const plan = await explain(await capture((db) => videosRepo.reapOrphans(db as never, OPTS)));

  expect(indexNames(plan)).toContain('videos_downloading_lease_idx');
  expect(seqScannedRelations(plan)).not.toContain('videos');
});

it('the job claim uses the status/created index', async () => {
  const plan = await explain(await capture((db) => jobsRepo.claimOne(db as never, OPTS)));

  expect(indexNames(plan)).toContain('jobs_status_created_idx');
  expect(seqScannedRelations(plan)).not.toContain('jobs');
});

it('the job reaper uses the partial enumerating index (F5)', async () => {
  const plan = await explain(await capture((db) => jobsRepo.reapOrphans(db as never)));

  expect(indexNames(plan)).toContain('jobs_enumerating_lease_idx');
  expect(seqScannedRelations(plan)).not.toContain('jobs');
});
