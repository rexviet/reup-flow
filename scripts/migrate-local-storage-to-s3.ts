/**
 * Moves finished downloads from the local disk (DATA_DIR) to S3, and back.
 *
 *   # from the repo root, nest stopped or started with LANES_ENABLED=false
 *   cd services/downloader
 *   pnpm exec ts-node --transpile-only --project tsconfig.json \
 *     ../../scripts/migrate-local-storage-to-s3.ts --dry-run
 *
 *   ... migrate-local-storage-to-s3.ts --lanes-stopped --backup-file ./reup.dump
 *   ... migrate-local-storage-to-s3.ts --revert --lanes-stopped --backup-file ./reup.dump
 *
 * Add `--aweme-id <id>` to any of the three to act on that one video only
 * (revert: only that video's map entry).
 *
 * (`--project tsconfig.json` matters: without it ts-node finds no config from
 * scripts/ and refuses the module settings.)
 *
 * Environment: DATABASE_URL, S3_ENDPOINT, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
 * and optionally S3_REGION, S3_BUCKET, S3_FORCE_PATH_STYLE, S3_KEY_PREFIX,
 * S3_STALE_UPLOAD_MS (same names and defaults the service reads), DATA_DIR.
 *
 * Needs migrations/261026-storage-migration-map.sql applied first.
 *
 * What it guarantees:
 *   - A video is moved by `S3Storage.put` with `expectedBytes` set to the local
 *     file's size, so a short upload is an error rather than a `done` row
 *     pointing at a truncated object.
 *   - The map row and the `storage_path` update commit in ONE transaction, so
 *     there is never a moved row without its way back.
 *   - Local files are NEVER deleted. Rollback is `--revert` plus
 *     STORAGE_BACKEND=local, and the files are still there for it.
 *   - `--revert` rewrites `storage_path` only for rows in the map, and only
 *     while the row still holds the path this script wrote. It never restores a
 *     database.
 *   - Real runs need `--lanes-stopped` (a lane writing `storage_path` while this
 *     rewrites it would lose one of the two updates) and `--backup-file`
 *     pointing at a non-empty dump, so a backup was at least made. A video
 *     still `downloading` refuses the run for the same reason.
 */
import { createReadStream } from 'node:fs';
import { createRequire } from 'node:module';
import { stat } from 'node:fs/promises';
import { isAbsolute, relative, resolve, sep } from 'node:path';
import { createS3Storage } from '../services/downloader/src/storage/s3.storage';
import type { Storage } from '../services/downloader/src/storage/storage.interface';

/** The slice of `pg.Pool` this script uses, so it can be driven by a test pool. */
export interface MigrationDb {
  query(text: string, params?: unknown[]): Promise<{ rows: unknown[]; rowCount: number | null }>;
  connect(): Promise<MigrationClient>;
}
export interface MigrationClient {
  query(text: string, params?: unknown[]): Promise<{ rows: unknown[]; rowCount: number | null }>;
  release(): void;
}

export type MigrationMode = 'dry-run' | 'migrate' | 'revert';

export interface MigrationReport {
  mode: MigrationMode;
  /** Rows the mode would act on: what the counting SQL below says. */
  candidates: number;
  /** Rows actually changed (always 0 for a dry run). */
  changed: number;
  /** One entry per row that could not be handled; the rest of the run continues. */
  failures: Array<{ awemeId: string; reason: string }>;
}

export interface MigrationOptions {
  db: MigrationDb;
  /** Needed only to migrate; dry-run and revert never touch S3. */
  storage?: Storage;
  mode: MigrationMode;
  dataDir: string;
  /** Limit the run to one video; without it every matching video is handled. */
  awemeId?: string;
  log?: (line: string) => void;
}

/**
 * The one definition of "a finished video still on the local disk". Used for the
 * dry-run count AND the work list, so the two cannot disagree.
 */
export const LOCAL_DONE_VIDEOS_SQL = `
  SELECT aweme_id, storage_path
    FROM videos
   WHERE status = 'done'
     AND storage_path IS NOT NULL
     AND storage_path NOT LIKE 's3://%'
   ORDER BY aweme_id`;

/** The same list narrowed to one video (`$1`), for a run that must touch only that row. */
export const LOCAL_DONE_VIDEO_SQL = LOCAL_DONE_VIDEOS_SQL.replace(
  'ORDER BY aweme_id',
  'AND aweme_id = $1\n   ORDER BY aweme_id',
);

export async function runMigration(opts: MigrationOptions): Promise<MigrationReport> {
  const log = opts.log ?? (() => undefined);
  if (opts.mode !== 'dry-run') await refuseIfLanesAreWriting(opts.db);
  return opts.mode === 'revert' ? revert(opts, log) : migrate(opts, log);
}

async function refuseIfLanesAreWriting(db: MigrationDb): Promise<void> {
  const { rows } = await db.query(`SELECT count(*)::int AS n FROM videos WHERE status = 'downloading'`);
  const downloading = (rows[0] as { n: number }).n;
  if (downloading > 0) {
    throw new Error(
      `refusing to run: ${downloading} video(s) are 'downloading', so a lane is still active. ` +
        'Stop the lanes (LANES_ENABLED=false) and let the reaper settle them first.',
    );
  }
}

async function migrate(opts: MigrationOptions, log: (line: string) => void): Promise<MigrationReport> {
  const { rows } = opts.awemeId
    ? await opts.db.query(LOCAL_DONE_VIDEO_SQL, [opts.awemeId])
    : await opts.db.query(LOCAL_DONE_VIDEOS_SQL);
  const report: MigrationReport = { mode: opts.mode, candidates: rows.length, changed: 0, failures: [] };
  log(`${rows.length} finished video(s) on local disk`);
  if (opts.mode === 'dry-run') return report;

  for (const row of rows as Array<{ aweme_id: string; storage_path: string }>) {
    try {
      await migrateOne(opts, row.aweme_id, row.storage_path);
      report.changed += 1;
      log(`moved ${row.aweme_id}`);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      report.failures.push({ awemeId: row.aweme_id, reason });
      log(`FAILED ${row.aweme_id}: ${reason}`);
    }
  }
  return report;
}

async function migrateOne(opts: MigrationOptions, awemeId: string, oldPath: string): Promise<void> {
  const { storage } = opts;
  if (!storage) throw new Error('a storage is required to migrate');
  const key = keyFor(opts.dataDir, oldPath);
  const size = (await stat(oldPath)).size;

  // One descriptor per video: released on every path, so a long run against a
  // store that refuses every put cannot exhaust them.
  const source = createReadStream(oldPath);
  let put;
  try {
    put = await storage.put(key, source, size);
  } catch (err) {
    source.destroy();
    throw err;
  }
  // Belt and braces after `put`'s own byte check: the object must be readable
  // under the final key before any row is pointed at it.
  if (put.bytes !== size || !(await storage.exists(key))) {
    throw new Error(`uploaded object for ${key} did not verify`);
  }

  const client = await opts.db.connect();
  try {
    await client.query('BEGIN');
    await client.query(
      `INSERT INTO storage_migration_map (aweme_id, old_path, new_path) VALUES ($1, $2, $3)`,
      [awemeId, oldPath, put.path],
    );
    // Guarded on the path we read: a row somebody changed meanwhile is theirs.
    const updated = await client.query(
      `UPDATE videos SET storage_path = $2
        WHERE aweme_id = $1 AND status = 'done' AND storage_path = $3`,
      [awemeId, put.path, oldPath],
    );
    if (updated.rowCount !== 1) {
      throw new Error('the video row changed while it was being moved; left as it was');
    }
    await client.query('COMMIT');
  } catch (err) {
    await client.query('ROLLBACK').catch(() => undefined);
    throw err;
  } finally {
    client.release();
  }
}

async function revert(opts: MigrationOptions, log: (line: string) => void): Promise<MigrationReport> {
  const { rows } = opts.awemeId
    ? await opts.db.query(
        `SELECT aweme_id, old_path, new_path FROM storage_migration_map WHERE aweme_id = $1`,
        [opts.awemeId],
      )
    : await opts.db.query(
        `SELECT aweme_id, old_path, new_path FROM storage_migration_map ORDER BY aweme_id`,
      );
  const report: MigrationReport = { mode: 'revert', candidates: rows.length, changed: 0, failures: [] };
  log(`${rows.length} migrated video(s) in the map`);

  for (const row of rows as Array<{ aweme_id: string; old_path: string; new_path: string }>) {
    const client = await opts.db.connect();
    try {
      // Pointing a row back at a file that is gone would trade a working S3
      // object for a dangling path. Leave the row, and its map entry, as they are.
      if (!(await stat(row.old_path).catch(() => undefined))?.isFile()) {
        throw new Error(`local file ${row.old_path} is missing; left on S3`);
      }
      await client.query('BEGIN');
      // Only while the row still holds what this script wrote.
      const restored = await client.query(
        `UPDATE videos SET storage_path = $2 WHERE aweme_id = $1 AND storage_path = $3`,
        [row.aweme_id, row.old_path, row.new_path],
      );
      if (restored.rowCount !== 1) {
        throw new Error('storage_path is no longer the migrated value; left as it is');
      }
      await client.query(`DELETE FROM storage_migration_map WHERE aweme_id = $1`, [row.aweme_id]);
      await client.query('COMMIT');
      report.changed += 1;
      log(`restored ${row.aweme_id}`);
    } catch (err) {
      await client.query('ROLLBACK').catch(() => undefined);
      const reason = err instanceof Error ? err.message : String(err);
      report.failures.push({ awemeId: row.aweme_id, reason });
      log(`FAILED ${row.aweme_id}: ${reason}`);
    } finally {
      client.release();
    }
  }
  return report;
}

/** The storage key a local path corresponds to: its path relative to DATA_DIR. */
export function keyFor(dataDir: string, localPath: string): string {
  if (!isAbsolute(localPath)) throw new Error(`stored path is not absolute: ${localPath}`);
  const rel = relative(resolve(dataDir), localPath);
  if (rel.length === 0 || rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error(`stored path is outside DATA_DIR (${dataDir}): ${localPath}`);
  }
  return rel.split(sep).join('/');
}

// ---------------------------------------------------------------------------
// Command line
// ---------------------------------------------------------------------------

function flag(argv: string[], name: string): boolean {
  return argv.includes(name);
}
function option(argv: string[], name: string): string | undefined {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
}
function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function storageFromEnv(): Storage {
  const prefix = process.env.S3_KEY_PREFIX ?? 'source/';
  if (!prefix.startsWith('source/') || !prefix.endsWith('/')) {
    throw new Error("S3_KEY_PREFIX must start with 'source/' and end with '/'");
  }
  const staleUploadMs = Number(process.env.S3_STALE_UPLOAD_MS ?? 86_400_000);
  if (!Number.isInteger(staleUploadMs) || staleUploadMs <= 0) {
    throw new Error('S3_STALE_UPLOAD_MS must be a positive integer');
  }
  return createS3Storage({
    endpoint: required('S3_ENDPOINT'),
    region: process.env.S3_REGION ?? 'us-east-1',
    forcePathStyle: (process.env.S3_FORCE_PATH_STYLE ?? 'true') !== 'false',
    accessKeyId: required('S3_ACCESS_KEY_ID'),
    secretAccessKey: required('S3_SECRET_ACCESS_KEY'),
    bucket: process.env.S3_BUCKET ?? 'reup-media',
    keyPrefix: prefix,
    staleUploadMs,
  });
}

async function main(argv: string[]): Promise<number> {
  const dryRun = flag(argv, '--dry-run');
  const isRevert = flag(argv, '--revert');
  if (dryRun && isRevert) throw new Error('--dry-run and --revert are exclusive');
  const mode: MigrationMode = dryRun ? 'dry-run' : isRevert ? 'revert' : 'migrate';
  const awemeId = option(argv, '--aweme-id');
  if (flag(argv, '--aweme-id') && !awemeId) throw new Error('--aweme-id needs a value');

  if (mode !== 'dry-run') {
    if (!flag(argv, '--lanes-stopped')) {
      throw new Error('pass --lanes-stopped to confirm nest runs with LANES_ENABLED=false (or is stopped)');
    }
    const backup = option(argv, '--backup-file');
    if (!backup) throw new Error('pass --backup-file <path> to a database dump taken just now');
    const info = await stat(backup).catch(() => undefined);
    if (!info || info.size === 0) {
      throw new Error(`--backup-file ${backup} does not exist or is empty`);
    }
  }

  // Only a migrate talks to S3: a dry run and a revert need the database and
  // the local disk, so they must work without S3 credentials.
  const storage = mode === 'migrate' ? storageFromEnv() : undefined;

  // `pg` is the downloader's dependency, not the repo root's.
  const requireFromDownloader = createRequire(resolve(__dirname, '../services/downloader/package.json'));
  const { Pool } = requireFromDownloader('pg') as { Pool: new (cfg: { connectionString: string }) => MigrationDb & { end(): Promise<void> } };
  const pool = new Pool({ connectionString: required('DATABASE_URL') });

  try {
    const report = await runMigration({
      db: pool,
      storage,
      mode,
      dataDir: process.env.DATA_DIR ?? '/data',
      awemeId,
      log: (line) => console.log(line),
    });
    console.log(
      `${mode}: ${report.candidates} candidate(s), ${report.changed} changed, ${report.failures.length} failed`,
    );
    return report.failures.length === 0 ? 0 : 1;
  } finally {
    await pool.end();
  }
}

if (require.main === module) {
  main(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (err) => {
      console.error(err instanceof Error ? err.message : String(err));
      process.exit(2);
    },
  );
}
