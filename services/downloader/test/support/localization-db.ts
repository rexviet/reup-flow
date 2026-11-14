import { randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { Pool } from 'pg';
import { FORWARD_MIGRATIONS, TEST_DATABASE_URL } from './db';
import { assertSafeDdlTarget } from './pg-target-guard';

const LOCALIZATION_MIGRATIONS = [
  '261026-localization-core',
  '261027-llm-cost',
  '261028-llm-call-provider',
  '261108-review-project-key',
  '261112-review-delivery',
  '261113-sequential-queue',
  '261114-auto-queue-switch',
].map((n) => resolve(__dirname, '../../../localization/migrations', `${n}.sql`));

export interface ScratchDatabase {
  pool: Pool;
  drop(): Promise<void>;
}

/**
 * A throwaway database holding the downloader's tables in `public` and the real
 * `localization.*` schema, both built from the checked-in migration files. The
 * per-schema helper in `db.ts` cannot do this: the dashboard SQL names
 * `localization.*` explicitly, and that schema name is global to a database.
 */
export async function createScratchDatabase(): Promise<ScratchDatabase> {
  const name = `test_dashboard_${process.pid}_${randomBytes(4).toString('hex')}`;
  const adminUrl = new URL(TEST_DATABASE_URL);
  assertSafeDdlTarget(TEST_DATABASE_URL, 'TEST_DATABASE_URL');
  adminUrl.pathname = '/postgres';
  const admin = new Pool({ connectionString: adminUrl.toString(), max: 1 });
  try {
    await admin.query(
      `DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'localization')
         THEN CREATE ROLE localization NOLOGIN; END IF; END $$`,
    );
    await admin.query(`CREATE DATABASE "${name}"`);
  } finally {
    await admin.end();
  }

  const url = new URL(TEST_DATABASE_URL);
  url.pathname = `/${name}`;
  assertSafeDdlTarget(url.toString(), 'scratch database');
  const pool = new Pool({ connectionString: url.toString(), max: 4 });
  for (const file of [...FORWARD_MIGRATIONS, ...LOCALIZATION_MIGRATIONS]) {
    await pool.query(readFileSync(file, 'utf8'));
  }
  return {
    pool,
    async drop() {
      await pool.end();
      const cleanup = new Pool({ connectionString: adminUrl.toString(), max: 1 });
      try {
        await cleanup.query(`DROP DATABASE IF EXISTS "${name}" WITH (FORCE)`);
      } finally {
        await cleanup.end();
      }
    },
  };
}
