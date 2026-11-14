/**
 * Phase 1 -- migration + schema-drift gate (phase-01 §1.1, §1.2, DoD 1).
 *
 * Two artifacts describe the same three tables: the checked-in migration SQL
 * (what an operator runs) and src/db/schema.ts (what every query compiles
 * against). Nothing in the type system ties them together, so they can drift
 * silently -- and the failure mode is a runtime "column does not exist" in a
 * lane at 3am, not a build error. These tests apply the real migration file
 * to an empty schema and compare the resulting catalog against the Drizzle
 * definition in both directions.
 */
import { getTableConfig } from 'drizzle-orm/pg-core';
import type { Pool } from 'pg';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  collections,
  dashboardActions,
  folderExports,
  jobs,
  localizationOutbox,
  profiles,
  publications,
  reviewDecisions,
  studioOpenRequests,
  videos,
} from '../../src/db/schema';
import {
  applyForwardMigration,
  applyRollbackMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testSchemaName,
} from '../support/db';
import { channelCollection } from '../support/collections';

jest.setTimeout(30_000);

const SCHEMA = testSchemaName('migration');
const ROLLBACK_FILE = resolve(__dirname, '../../migrations/261112-dashboard-intake.down.sql');

const ALL_TABLES = [
  profiles,
  jobs,
  videos,
  localizationOutbox,
  reviewDecisions,
  publications,
  studioOpenRequests,
  dashboardActions,
  collections,
  folderExports,
];
const EXPECTED_TABLES = [
  'collections',
  'dashboard_actions',
  'folder_exports',
  'jobs',
  'localization_outbox',
  'profiles',
  'publications',
  'review_decisions',
  'studio_open_requests',
  'videos',
];

/** Collapses catalog vs Drizzle type spellings onto one comparable form. */
function normalizeType(sqlType: string): string {
  // bigserial is a bigint column plus a sequence default; the catalog reports bigint.
  return sqlType.toLowerCase().replace(/\s+/g, ' ').trim().replace(/^bigserial$/, 'bigint');
}

async function tableNames(pool: Pool): Promise<string[]> {
  const { rows } = await pool.query<{ table_name: string }>(
    `SELECT table_name FROM information_schema.tables
      WHERE table_schema = $1 AND table_type = 'BASE TABLE'
      ORDER BY table_name`,
    [SCHEMA],
  );
  return rows.map((r) => r.table_name);
}

describe('phase 1: init migration and schema.ts agree', () => {
  let pool: Pool;

  beforeAll(async () => {
    await createEmptySchema(SCHEMA);
    pool = createTestPool(SCHEMA);
    await applyForwardMigration(pool);
  });

  afterAll(async () => {
    await pool.end();
    await dropSchema(SCHEMA);
  });

  it('creates exactly the tables the design specifies', async () => {
    expect(await tableNames(pool)).toEqual(EXPECTED_TABLES);
  });

  describe.each([
    ['profiles', profiles],
    ['jobs', jobs],
    ['videos', videos],
    ['localization_outbox', localizationOutbox],
    ['review_decisions', reviewDecisions],
    ['publications', publications],
    ['studio_open_requests', studioOpenRequests],
    ['dashboard_actions', dashboardActions],
    ['collections', collections],
    ['folder_exports', folderExports],
  ])('%s columns match the migration', (tableName, table) => {
    it('has the same column set, types and nullability in both directions', async () => {
      const { rows } = await pool.query<{
        column_name: string;
        data_type: string;
        is_nullable: 'YES' | 'NO';
      }>(
        `SELECT column_name, data_type, is_nullable
           FROM information_schema.columns
          WHERE table_schema = $1 AND table_name = $2`,
        [SCHEMA, tableName],
      );

      const fromDb = Object.fromEntries(
        rows.map((r) => [
          r.column_name,
          { type: normalizeType(r.data_type), notNull: r.is_nullable === 'NO' },
        ]),
      );
      const fromDrizzle = Object.fromEntries(
        getTableConfig(table).columns.map((c) => [
          c.name,
          { type: normalizeType(c.getSQLType()), notNull: c.notNull },
        ]),
      );

      // toEqual on the whole map, not a per-column loop: this fails on an
      // extra column in EITHER artifact, which a loop over one side misses.
      expect(fromDb).toEqual(fromDrizzle);
    });
  });

  it('creates every index schema.ts declares, and no others', async () => {
    const declared = ALL_TABLES.flatMap((table) =>
      getTableConfig(table).indexes.map((idx) => idx.config.name!),
    );

    const { rows } = await pool.query<{ indexname: string; indexdef: string }>(
      `SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1`,
      [SCHEMA],
    );
    // Primary-key and unique-constraint indexes are created by their
    // constraint, not by an index() call in schema.ts, so they are not part
    // of this comparison.
    const pkNames = new Set([
      ...ALL_TABLES.map((t) => `${getTableConfig(t).name}_pkey`),
      ...ALL_TABLES.flatMap((t) => getTableConfig(t).uniqueConstraints.map((u) => u.name!)),
    ]);
    const actual = rows.filter((r) => !pkNames.has(r.indexname));

    expect(actual.map((r) => r.indexname).sort()).toEqual([...declared].sort());
  });

  it('creates the three partial indexes as partial, not as full-table indexes', async () => {
    // A partial index that silently loses its WHERE clause still answers
    // every query correctly -- it just scans the whole table instead of only
    // the outstanding work. Nothing else in this suite would notice.
    const expectedPredicates: Record<string, string> = {
      videos_pending_claim_idx: "status = 'pending'",
      videos_downloading_lease_idx: "status = 'downloading'",
      jobs_enumerating_lease_idx: "status = 'enumerating'",
      localization_outbox_due_idx: "state = 'pending'",
      folder_exports_open_idx: "state = ANY (ARRAY['pending', 'exporting'])",
      folder_exports_one_open_idx: "state = ANY (ARRAY['pending', 'exporting'])",
    };

    const { rows } = await pool.query<{ indexname: string; indexdef: string }>(
      `SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1`,
      [SCHEMA],
    );
    const defs = Object.fromEntries(rows.map((r) => [r.indexname, r.indexdef]));

    for (const [name, predicate] of Object.entries(expectedPredicates)) {
      expect(defs[name]).toBeDefined();
      expect(defs[name]!.toLowerCase()).toContain('where');
      expect(defs[name]!.replace(/::text/g, '')).toContain(predicate);
    }
  });

  it('indexes the columns the claim and reaper paths actually order by', async () => {
    // Names alone are not enough. An index on (create_time, status) instead
    // of (status, create_time) keeps its name, stays partial, and is still
    // chosen by the planner -- it just can't serve the ordered claim
    // efficiently. Assert the shape, not the label.
    const expectedColumns: Record<string, string> = {
      videos_pending_claim_idx: '(status, create_time)',
      videos_downloading_lease_idx: '(status, lease_expires_at)',
      videos_profile_recent_idx: '(sec_user_id, create_time DESC)',
      jobs_status_created_idx: '(status, created_at)',
      jobs_enumerating_lease_idx: '(status, lease_expires_at)',
      jobs_sec_user_id_idx: '(sec_user_id)',
      localization_outbox_due_idx: '(next_attempt_at)',
      dashboard_actions_aweme_idx: '(aweme_id, created_at DESC)',
      videos_collection_recent_idx: '(collection_id, create_time DESC)',
      folder_exports_open_idx: '(id)',
      folder_exports_one_open_idx: '(aweme_id, generation)',
    };

    const { rows } = await pool.query<{ indexname: string; indexdef: string }>(
      `SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1`,
      [SCHEMA],
    );
    const defs = Object.fromEntries(rows.map((r) => [r.indexname, r.indexdef]));

    for (const [name, columns] of Object.entries(expectedColumns)) {
      expect(defs[name]).toBeDefined();
      expect(defs[name]).toContain(columns);
    }
  });

  it('creates every unique constraint schema.ts declares', async () => {
    const declared = ALL_TABLES.flatMap((t) =>
      getTableConfig(t).uniqueConstraints.map((u) => u.name!),
    );
    const { rows } = await pool.query<{ conname: string }>(
      `SELECT c.conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE n.nspname = $1 AND c.contype = 'u'`,
      [SCHEMA],
    );
    expect(rows.map((r) => r.conname).sort()).toEqual([...declared].sort());
  });

  it('accepts held outbox state and rejects unknown states', async () => {
    const collection = await channelCollection(pool, 'p');
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, collection_id)
       VALUES ('v-held', 'p', now(), $1), ('v-bad', 'p', now(), $1)`,
      [collection],
    );
    await pool.query(`INSERT INTO localization_outbox (aweme_id, state) VALUES ('v-held', 'held')`);
    await expect(
      pool.query(`INSERT INTO localization_outbox (aweme_id, state) VALUES ('v-bad', 'bogus')`),
    ).rejects.toThrow(/localization_outbox_state_check/);
    await pool.query(`DELETE FROM localization_outbox`);
  });

  it('refuses to roll back while a held row exists', async () => {
    const collection = await channelCollection(pool, 'p-held');
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, collection_id)
       VALUES ('v-held2', 'p-held', now(), $1) ON CONFLICT DO NOTHING`,
      [collection],
    );
    await pool.query(`INSERT INTO localization_outbox (aweme_id, state) VALUES ('v-held2', 'held')`);
    // The down file opens its own transaction and aborts it on error; use a
    // dedicated client and roll back so no pooled connection stays aborted.
    const client = await pool.connect();
    try {
      await expect(client.query(readFileSync(ROLLBACK_FILE, 'utf8'))).rejects.toThrow(/held rows/);
    } finally {
      await client.query('ROLLBACK');
      client.release();
    }
    expect(await tableNames(pool)).toEqual(EXPECTED_TABLES);
    await pool.query(`DELETE FROM localization_outbox`);
  });

  it('rolls back to a completely empty schema, and forward again', async () => {
    await applyRollbackMigration(pool);
    expect(await tableNames(pool)).toEqual([]);

    // Re-runnable: an operator who rolls back and re-applies must not need to
    // hand-clean leftovers first.
    await applyForwardMigration(pool);
    expect(await tableNames(pool)).toEqual(EXPECTED_TABLES);
  });
});

describe('phase 1: F7 -- videos is keyed globally, with no job_id', () => {
  // F7 is the one decision the plan says to record as documentation rather
  // than code (phase-01 §1.4). Documentation alone rots, and the "fix" it is
  // there to prevent is someone adding `job_id` to make per-job progress
  // deltas work -- which would destroy the global dedupe that makes
  // incremental re-runs free. So: assert the column never appears, and
  // assert the explanation is still sitting where the next reader will hit
  // it before they reach for the migration.
  const schemaSource = readFileSync(
    resolve(__dirname, '../../src/db/schema.ts'),
    'utf8',
  );

  it('declares no job_id column on videos', () => {
    const columns = getTableConfig(videos).columns.map((c) => c.name);
    expect(columns).not.toContain('job_id');
  });

  it('explains the omission in a comment attached to the videos table', () => {
    const beforeVideos = schemaSource.slice(0, schemaSource.indexOf('export const videos'));
    const comment = beforeVideos.slice(beforeVideos.lastIndexOf('/**'));
    expect(comment).toMatch(/job_id/);
    expect(comment).toMatch(/F7/);
  });
});
