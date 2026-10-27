/**
 * scripts/migrate-local-storage-to-s3.ts against real Postgres (a per-run
 * schema) and real MinIO. Assertions read the database, the bucket and the
 * disk, never the calls the script made.
 */
import { mkdtemp, readFile, rm, writeFile, mkdir, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import type { Pool } from 'pg';
import type { S3Client } from '@aws-sdk/client-s3';
import {
  LOCAL_DONE_VIDEOS_SQL,
  keyFor,
  runMigration,
} from '../../../../scripts/migrate-local-storage-to-s3';
import { S3Storage } from '../../src/storage/s3.storage';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testSchemaName,
} from '../support/db';
import {
  getObjectBytes,
  listObjectKeys,
  makeS3Client,
  readS3TestEnv,
  uniqueTestPrefix,
  wipePrefix,
} from '../support/s3-test-env';
import { readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';

jest.setTimeout(60_000);

const SCHEMA = testSchemaName('migrate_s3');
const MAP_MIGRATION = resolve(__dirname, '../../migrations/261026-storage-migration-map.sql');
const env = readS3TestEnv();

interface Seed {
  awemeId: string;
  secUserId: string;
  status: string;
  /** Local file to create and point the row at; `null` leaves storage_path NULL. */
  content: Buffer | null;
  /** Point the row at a path but never create the file. */
  missingFile?: boolean;
  /** An explicit storage_path, overriding the generated local one. */
  storagePath?: string;
}

describe('migrate-local-storage-to-s3', () => {
  let pool: Pool;
  let client: S3Client;
  let dataDir: string;
  let prefix: string;
  let storage: S3Storage;

  beforeAll(async () => {
    await createEmptySchema(SCHEMA);
    pool = createTestPool(SCHEMA);
    await applyForwardMigration(pool);
    await pool.query(readFileSync(MAP_MIGRATION, 'utf8'));
    client = makeS3Client(env);
  });

  afterAll(async () => {
    await pool.end();
    await dropSchema(SCHEMA);
    client.destroy();
  });

  beforeEach(async () => {
    await pool.query('TRUNCATE videos, storage_migration_map CASCADE');
    dataDir = await mkdtemp(join(tmpdir(), 'reup-migrate-s3-'));
    prefix = uniqueTestPrefix('migrate');
    storage = new S3Storage(client, { bucket: env.bucket, keyPrefix: prefix, staleUploadMs: 86_400_000 });
  });

  afterEach(async () => {
    await rm(dataDir, { recursive: true, force: true });
    await wipePrefix(client, env.bucket, prefix);
  });

  /** Inserts a row, writing its local file unless told otherwise. Returns the stored path. */
  async function seed(row: Seed): Promise<string | null> {
    let path: string | null = row.storagePath ?? null;
    if (path === null && (row.content !== null || row.missingFile)) {
      path = join(dataDir, 'douyin', row.secUserId, `${row.awemeId}.mp4`);
    }
    if (row.content !== null && !row.missingFile && path && !row.storagePath) {
      await mkdir(dirname(path), { recursive: true });
      await writeFile(path, row.content);
    }
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, storage_path)
       VALUES ($1, $2, now(), $3, $4)`,
      [row.awemeId, row.secUserId, row.status, path],
    );
    return path;
  }

  async function storagePaths(): Promise<Record<string, string | null>> {
    const { rows } = await pool.query('SELECT aweme_id, storage_path FROM videos ORDER BY aweme_id');
    return Object.fromEntries(rows.map((r) => [r.aweme_id, r.storage_path]));
  }

  async function seedMixedLibrary(): Promise<{ originals: Record<string, string | null>; bodies: Record<string, Buffer> }> {
    const bodies = {
      '1001': Buffer.alloc(3000, 0x31),
      '1002': Buffer.alloc(5 * 1024 * 1024 + 17, 0x32),
      '1003': Buffer.alloc(10, 0x33),
    };
    await seed({ awemeId: '1001', secUserId: 'sec_a', status: 'done', content: bodies['1001'] });
    await seed({ awemeId: '1002', secUserId: 'sec_a', status: 'done', content: bodies['1002'] });
    await seed({ awemeId: '1003', secUserId: 'sec_b', status: 'done', content: bodies['1003'] });
    // Not candidates: already on S3, never downloaded, failed.
    await seed({ awemeId: '2001', secUserId: 'sec_b', status: 'done', content: null, storagePath: 's3://reup-media/source/other/2001.mp4' });
    await seed({ awemeId: '2002', secUserId: 'sec_b', status: 'pending', content: null });
    await seed({ awemeId: '2003', secUserId: 'sec_b', status: 'failed', content: null });
    return { originals: await storagePaths(), bodies };
  }

  const run = (mode: 'dry-run' | 'migrate' | 'revert') =>
    runMigration({ db: pool, storage, mode, dataDir });

  async function countByIndependentSql(): Promise<number> {
    const { rows } = await pool.query(
      `SELECT count(*)::int AS n FROM videos
        WHERE status = 'done' AND storage_path IS NOT NULL AND storage_path NOT LIKE 's3://%'`,
    );
    return rows[0].n;
  }

  describe('--dry-run', () => {
    it('reports the same count as a plain SQL count and changes nothing', async () => {
      const { originals } = await seedMixedLibrary();
      const expected = await countByIndependentSql();
      expect(expected).toBe(3);

      const report = await run('dry-run');

      expect(report).toMatchObject({ mode: 'dry-run', candidates: expected, changed: 0, failures: [] });
      expect(await storagePaths()).toEqual(originals);
      expect(await listObjectKeys(client, env.bucket, prefix)).toEqual([]);
      expect((await pool.query('SELECT count(*)::int AS n FROM storage_migration_map')).rows[0].n).toBe(0);
    });

    it('uses one definition of a candidate for the count and the work list', async () => {
      await seedMixedLibrary();
      const { rows } = await pool.query(LOCAL_DONE_VIDEOS_SQL);
      expect(rows.map((r) => r.aweme_id)).toEqual(['1001', '1002', '1003']);
    });

    it('needs no storage: a dry run never talks to S3', async () => {
      await seedMixedLibrary();
      await expect(runMigration({ db: pool, mode: 'dry-run', dataDir })).resolves.toMatchObject({
        candidates: 3,
        changed: 0,
      });
    });

    it('is allowed while a lane is active, since it writes nothing', async () => {
      await seed({ awemeId: '3001', secUserId: 'sec_a', status: 'downloading', content: null });
      await expect(run('dry-run')).resolves.toMatchObject({ candidates: 0 });
    });
  });

  describe('migrate', () => {
    it('uploads each file byte for byte, repoints the row, records the map, and keeps the local file', async () => {
      const { originals, bodies } = await seedMixedLibrary();

      const report = await run('migrate');

      expect(report).toMatchObject({ mode: 'migrate', candidates: 3, changed: 3, failures: [] });
      const after = await storagePaths();
      for (const id of ['1001', '1002', '1003'] as const) {
        const local = originals[id] as string;
        const key = keyFor(dataDir, local);
        expect(after[id]).toBe(`s3://${env.bucket}/${prefix}${key}`);
        const stored = await getObjectBytes(client, env.bucket, `${prefix}${key}`);
        expect({ length: stored.length, same: stored.equals(bodies[id]) }).toEqual({
          length: bodies[id].length,
          same: true,
        });
        // Never deleted: this is the way back.
        expect((await readFile(local)).equals(bodies[id])).toBe(true);
      }
      // Untouched rows stay untouched.
      expect(after['2001']).toBe(originals['2001']);
      expect(after['2002']).toBeNull();
      expect(after['2003']).toBeNull();

      const map = (await pool.query('SELECT aweme_id, old_path, new_path FROM storage_migration_map ORDER BY aweme_id')).rows;
      expect(map).toEqual(
        ['1001', '1002', '1003'].map((id) => ({
          aweme_id: id,
          old_path: originals[id],
          new_path: after[id],
        })),
      );
    });

    it('reports a missing local file, leaves that row alone, and still moves the rest', async () => {
      await seed({ awemeId: '1001', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(100, 1) });
      const lostPath = await seed({ awemeId: '1002', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(1), missingFile: true });
      await seed({ awemeId: '1003', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(100, 3) });

      const report = await run('migrate');

      expect(report.changed).toBe(2);
      expect(report.failures).toHaveLength(1);
      expect(report.failures[0].awemeId).toBe('1002');
      const after = await storagePaths();
      expect(after['1002']).toBe(lostPath);
      expect(after['1001']).toMatch(/^s3:\/\//);
      expect(after['1003']).toMatch(/^s3:\/\//);
      expect((await pool.query(`SELECT 1 FROM storage_migration_map WHERE aweme_id = '1002'`)).rowCount).toBe(0);
    });

    it('refuses a row whose path lies outside DATA_DIR and leaves it alone', async () => {
      const outside = await mkdtemp(join(tmpdir(), 'reup-outside-'));
      try {
        const stray = join(outside, 'stray.mp4');
        await writeFile(stray, Buffer.alloc(64, 9));
        await seed({ awemeId: '1001', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(1), storagePath: stray });

        const report = await run('migrate');

        expect(report.changed).toBe(0);
        expect(report.failures).toEqual([{ awemeId: '1001', reason: expect.stringContaining('outside DATA_DIR') }]);
        expect((await storagePaths())['1001']).toBe(stray);
        expect(await listObjectKeys(client, env.bucket, prefix)).toEqual([]);
      } finally {
        await rm(outside, { recursive: true, force: true });
      }
    });

    it('releases every file descriptor when the store refuses each upload', async () => {
      for (let i = 0; i < 40; i++) {
        await seed({ awemeId: `${5000 + i}`, secUserId: 'sec_f', status: 'done', content: Buffer.alloc(64, i) });
      }
      // The downloader credential may only write under source/.
      const denied = new S3Storage(client, {
        bucket: env.bucket,
        keyPrefix: `work/it-denied-${Date.now()}/`,
        staleUploadMs: 86_400_000,
      });
      const openFds = () => readdirSync('/dev/fd').length;
      const before = openFds();

      const report = await runMigration({ db: pool, storage: denied, mode: 'migrate', dataDir });
      // Descriptors of destroyed streams close on the next ticks.
      await new Promise((resolve) => setTimeout(resolve, 200));

      expect(report.failures).toHaveLength(40);
      expect(report.changed).toBe(0);
      expect(openFds() - before).toBeLessThan(5);
    });

    it('is idempotent: a second run finds nothing left to move', async () => {
      await seedMixedLibrary();
      await run('migrate');
      const afterFirst = await storagePaths();

      const second = await run('migrate');

      expect(second).toMatchObject({ candidates: 0, changed: 0, failures: [] });
      expect(await storagePaths()).toEqual(afterFirst);
    });

    it.each(['migrate', 'revert'] as const)('refuses to %s while a video is downloading', async (mode) => {
      await seed({ awemeId: '1001', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(10) });
      await seed({ awemeId: '3001', secUserId: 'sec_a', status: 'downloading', content: null });
      const before = await storagePaths();

      await expect(run(mode)).rejects.toThrow(/downloading/);

      expect(await storagePaths()).toEqual(before);
      expect(await listObjectKeys(client, env.bucket, prefix)).toEqual([]);
    });
  });

  describe('--aweme-id', () => {
    it('migrates only that video: the others keep their local path and nothing else lands in the bucket', async () => {
      const { originals, bodies } = await seedMixedLibrary();

      const report = await runMigration({ db: pool, storage, mode: 'migrate', dataDir, awemeId: '1002' });

      expect(report).toMatchObject({ mode: 'migrate', candidates: 1, changed: 1, failures: [] });
      const after = await storagePaths();
      const key = keyFor(dataDir, originals['1002'] as string);
      expect(after['1002']).toBe(`s3://${env.bucket}/${prefix}${key}`);
      expect((await getObjectBytes(client, env.bucket, `${prefix}${key}`)).equals(bodies['1002'])).toBe(true);
      for (const id of ['1001', '1003', '2001'] as const) expect(after[id]).toBe(originals[id]);
      expect(await listObjectKeys(client, env.bucket, prefix)).toEqual([`${prefix}${key}`]);
      expect((await pool.query('SELECT aweme_id FROM storage_migration_map')).rows).toEqual([{ aweme_id: '1002' }]);
    });

    it('counts one candidate in a dry run and none for an id that is not a local finished video', async () => {
      await seedMixedLibrary();
      await expect(runMigration({ db: pool, mode: 'dry-run', dataDir, awemeId: '1001' })).resolves.toMatchObject({
        candidates: 1,
        changed: 0,
      });
      await expect(runMigration({ db: pool, mode: 'dry-run', dataDir, awemeId: '2002' })).resolves.toMatchObject({
        candidates: 0,
      });
      await expect(runMigration({ db: pool, mode: 'dry-run', dataDir, awemeId: 'no-such-id' })).resolves.toMatchObject({
        candidates: 0,
      });
    });

    it('reverts only that video, leaving the other migrated rows on S3', async () => {
      const { originals } = await seedMixedLibrary();
      await run('migrate');
      const migrated = await storagePaths();

      const report = await runMigration({ db: pool, mode: 'revert', dataDir, awemeId: '1001' });

      expect(report).toMatchObject({ mode: 'revert', candidates: 1, changed: 1, failures: [] });
      const after = await storagePaths();
      expect(after['1001']).toBe(originals['1001']);
      expect(after['1002']).toBe(migrated['1002']);
      expect(after['1003']).toBe(migrated['1003']);
      expect((await pool.query('SELECT aweme_id FROM storage_migration_map ORDER BY aweme_id')).rows).toEqual([
        { aweme_id: '1002' },
        { aweme_id: '1003' },
      ]);
    });
  });

  describe('--revert', () => {
    it('restores exactly the original paths and empties the map, leaving the local files', async () => {
      const { originals } = await seedMixedLibrary();
      await run('migrate');

      const report = await run('revert');

      expect(report).toMatchObject({ mode: 'revert', candidates: 3, changed: 3, failures: [] });
      expect(await storagePaths()).toEqual(originals);
      expect((await pool.query('SELECT count(*)::int AS n FROM storage_migration_map')).rows[0].n).toBe(0);
      for (const id of ['1001', '1002', '1003']) {
        await expect(access(originals[id] as string)).resolves.toBeUndefined();
      }
    });

    it('never touches a row that is not in the map', async () => {
      await seed({ awemeId: '1001', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(10) });
      await seed({ awemeId: '2001', secUserId: 'sec_a', status: 'done', content: null, storagePath: 's3://elsewhere/2001.mp4' });
      await run('migrate');

      await run('revert');

      expect((await storagePaths())['2001']).toBe('s3://elsewhere/2001.mp4');
    });

    it('leaves a row alone, and says so, when its path changed after the migration', async () => {
      await seed({ awemeId: '1001', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(10, 1) });
      await seed({ awemeId: '1002', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(10, 2) });
      await run('migrate');
      await pool.query(`UPDATE videos SET storage_path = 's3://later/rewritten.mp4' WHERE aweme_id = '1002'`);

      const report = await run('revert');

      expect(report.changed).toBe(1);
      expect(report.failures).toEqual([{ awemeId: '1002', reason: expect.stringContaining('no longer') }]);
      expect((await storagePaths())['1002']).toBe('s3://later/rewritten.mp4');
      expect((await pool.query(`SELECT aweme_id FROM storage_migration_map`)).rows).toEqual([{ aweme_id: '1002' }]);
    });

    it('leaves a row on S3, with its map entry, when the local file is gone', async () => {
      await seed({ awemeId: '1001', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(10, 1) });
      await seed({ awemeId: '1002', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(10, 2) });
      const originals = await storagePaths();
      await run('migrate');
      const migrated = await storagePaths();
      await rm(originals['1002'] as string);

      const report = await runMigration({ db: pool, mode: 'revert', dataDir });

      expect(report.changed).toBe(1);
      expect(report.failures).toEqual([{ awemeId: '1002', reason: expect.stringContaining('missing') }]);
      const after = await storagePaths();
      expect(after['1001']).toBe(originals['1001']);
      expect(after['1002']).toBe(migrated['1002']);
      expect((await pool.query(`SELECT aweme_id FROM storage_migration_map`)).rows).toEqual([{ aweme_id: '1002' }]);
    });

    it('needs no storage at all: a revert never talks to S3', async () => {
      await seed({ awemeId: '1001', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(10, 1) });
      const originals = await storagePaths();
      await run('migrate');

      await runMigration({ db: pool, mode: 'revert', dataDir });

      expect(await storagePaths()).toEqual(originals);
    });

    it('can migrate again after a revert', async () => {
      await seed({ awemeId: '1001', secUserId: 'sec_a', status: 'done', content: Buffer.alloc(10, 1) });
      await run('migrate');
      await run('revert');

      const again = await run('migrate');

      expect(again).toMatchObject({ candidates: 1, changed: 1, failures: [] });
      expect((await storagePaths())['1001']).toMatch(/^s3:\/\//);
    });
  });

  describe('keyFor', () => {
    it('maps a path under DATA_DIR to its relative key', () => {
      expect(keyFor('/data', '/data/douyin/sec/1.mp4')).toBe('douyin/sec/1.mp4');
    });

    it.each(['relative/1.mp4', '/elsewhere/1.mp4', '/data/../etc/passwd', '/data'])('rejects %s', (path) => {
      expect(() => keyFor('/data', path)).toThrow();
    });
  });
});
