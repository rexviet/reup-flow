/**
 * localization_outbox -- the invariant is "a `done` video always has an outbox
 * row". Runs against real Postgres: atomicity across two statements and
 * search_path isolation have no meaningful in-memory equivalent.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { markDone } from '../../src/db/videos.repository';
import { localizationOutbox, videos } from '../../src/db/schema';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';

jest.setTimeout(30_000);

const SCHEMA = testSchemaName('outbox');
const OTHER_SCHEMA = testSchemaName('outbox_other');

let pool: Pool;
let db: ReturnType<typeof testDb>;

async function seedDownloading(testDbHandle: ReturnType<typeof testDb>, awemeId: string) {
  await testDbHandle.insert(videos).values({
    awemeId,
    secUserId: 'profile-1',
    createTime: new Date(Date.UTC(2026, 0, 1)),
    status: 'downloading',
  });
}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  db = testDb(pool);
  await applyForwardMigration(pool);
});

afterAll(async () => {
  await pool.end();
  await dropSchema(SCHEMA);
});

beforeEach(async () => {
  await truncateAll(pool);
});

describe('markDone writes the outbox row', () => {
  it('leaves the video done with exactly one outbox row, in the test schema', async () => {
    await seedDownloading(db, 'v1');
    await markDone(db, 'v1', 's3://bucket/v1.mp4');

    const [video] = await db.select().from(videos).where(eq(videos.awemeId, 'v1'));
    expect(video!.status).toBe('done');

    const rows = await db.select().from(localizationOutbox);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      awemeId: 'v1',
      generation: 1,
      state: 'pending',
      dispatchAttempts: 0,
    });

    // The row must live in the per-run schema, not `public`, which the running
    // compose stack uses.
    const { rows: where } = await pool.query<{ table_schema: string }>(
      `SELECT table_schema FROM information_schema.tables WHERE table_name = 'localization_outbox'
        AND table_schema = $1`,
      [SCHEMA],
    );
    expect(where).toHaveLength(1);
    const { rows: viaPath } = await pool.query<{ n: number }>(
      `SELECT count(*)::int AS n FROM localization_outbox WHERE aweme_id = 'v1'`,
    );
    expect(viaPath[0]!.n).toBe(1);
  });

  it('keeps a single row when called twice, and does not reset a re-queued generation', async () => {
    await seedDownloading(db, 'v1');
    await markDone(db, 'v1', 's3://bucket/v1.mp4');
    await pool.query(`UPDATE localization_outbox SET generation = 2, state = 'dispatched'`);

    await markDone(db, 'v1', 's3://bucket/v1.mp4');

    const rows = await db.select().from(localizationOutbox);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ generation: 2, state: 'dispatched' });
  });

  it('keeps the video downloading when the outbox insert fails', async () => {
    await seedDownloading(db, 'v1');
    // A real failure at the INSERT, not a stubbed call.
    await pool.query(`
      CREATE FUNCTION outbox_insert_fails() RETURNS trigger LANGUAGE plpgsql AS
        $$ BEGIN RAISE EXCEPTION 'outbox insert refused'; END $$;
      CREATE TRIGGER outbox_insert_fails BEFORE INSERT ON localization_outbox
        FOR EACH ROW EXECUTE FUNCTION outbox_insert_fails();
    `);
    try {
      await expect(markDone(db, 'v1', 's3://bucket/v1.mp4')).rejects.toThrow();
    } finally {
      await pool.query(`
        DROP TRIGGER outbox_insert_fails ON localization_outbox;
        DROP FUNCTION outbox_insert_fails();
      `);
    }

    const [video] = await db.select().from(videos).where(eq(videos.awemeId, 'v1'));
    expect(video!.status).toBe('downloading');
    expect(video!.storagePath).toBeNull();
    expect(await db.select().from(localizationOutbox)).toHaveLength(0);
  });

  it('keeps atomicity when the caller already holds a transaction', async () => {
    await seedDownloading(db, 'v1');
    await pool.query(`
      CREATE FUNCTION outbox_insert_fails() RETURNS trigger LANGUAGE plpgsql AS
        $$ BEGIN RAISE EXCEPTION 'outbox insert refused'; END $$;
      CREATE TRIGGER outbox_insert_fails BEFORE INSERT ON localization_outbox
        FOR EACH ROW EXECUTE FUNCTION outbox_insert_fails();
    `);
    try {
      await expect(
        db.transaction(async (tx) => {
          await markDone(tx, 'v1', 's3://bucket/v1.mp4');
        }),
      ).rejects.toThrow();
    } finally {
      await pool.query(`
        DROP TRIGGER outbox_insert_fails ON localization_outbox;
        DROP FUNCTION outbox_insert_fails();
      `);
    }

    const [video] = await db.select().from(videos).where(eq(videos.awemeId, 'v1'));
    expect(video!.status).toBe('downloading');
  });
});

describe('overlapping runs', () => {
  it('do not see each other outbox rows', async () => {
    await createEmptySchema(OTHER_SCHEMA);
    const otherPool = createTestPool(OTHER_SCHEMA);
    try {
      await applyForwardMigration(otherPool);
      const otherDb = testDb(otherPool);

      await seedDownloading(db, 'mine');
      await seedDownloading(otherDb, 'theirs');
      await markDone(db, 'mine', 's3://bucket/mine.mp4');
      await markDone(otherDb, 'theirs', 's3://bucket/theirs.mp4');

      const mine = await db.select().from(localizationOutbox);
      const theirs = await otherDb.select().from(localizationOutbox);
      expect(mine.map((r) => r.awemeId)).toEqual(['mine']);
      expect(theirs.map((r) => r.awemeId)).toEqual(['theirs']);

      // Nothing leaked into `public`, which the running stack uses. The table
      // may not exist there at all, so check with to_regclass before reading.
      const { rows: reg } = await pool.query<{ exists: boolean }>(
        `SELECT to_regclass('public.localization_outbox') IS NOT NULL AS exists`,
      );
      if (reg[0]!.exists) {
        const { rows: leaked } = await pool.query(
          `SELECT aweme_id FROM public.localization_outbox WHERE aweme_id IN ('mine', 'theirs')`,
        );
        expect(leaked).toEqual([]);
      }
    } finally {
      await otherPool.end();
      await dropSchema(OTHER_SCHEMA);
    }
  });
});
