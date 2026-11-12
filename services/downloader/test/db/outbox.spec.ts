/**
 * localization_outbox -- the invariant is "a `done` video always has an outbox
 * row". Runs against real Postgres: atomicity across two statements and
 * search_path isolation have no meaningful in-memory equivalent.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { markDone } from '../../src/db/videos.repository';
import { localizationOutbox, profiles, videos } from '../../src/db/schema';
import { localize } from '../../src/dashboard/intake.repository';
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
      state: 'held',
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

async function stateOf(awemeId: string): Promise<string | undefined> {
  const [row] = await db
    .select({ state: localizationOutbox.state })
    .from(localizationOutbox)
    .where(eq(localizationOutbox.awemeId, awemeId));
  return row?.state;
}

async function seedChannel(autoLocalize: boolean): Promise<void> {
  await db.insert(profiles).values({ secUserId: 'profile-1', autoLocalize });
}

describe('markDone chooses held or pending', () => {
  it('holds a video whose channel switch is off', async () => {
    await seedChannel(false);
    await seedDownloading(db, 'v1');
    await markDone(db, 'v1', 's3://bucket/v1.mp4');
    expect(await stateOf('v1')).toBe('held');
  });

  it('queues a video whose channel switch is on', async () => {
    await seedChannel(true);
    await seedDownloading(db, 'v1');
    await markDone(db, 'v1', 's3://bucket/v1.mp4');
    expect(await stateOf('v1')).toBe('pending');
  });

  it('lets the video override win over the channel in both directions', async () => {
    await seedChannel(false);
    await seedDownloading(db, 'on');
    await db.update(videos).set({ localizeOnDone: true }).where(eq(videos.awemeId, 'on'));
    await markDone(db, 'on', 's3://bucket/on.mp4');
    expect(await stateOf('on')).toBe('pending');

    await db.update(profiles).set({ autoLocalize: true });
    await seedDownloading(db, 'off');
    await db.update(videos).set({ localizeOnDone: false }).where(eq(videos.awemeId, 'off'));
    await markDone(db, 'off', 's3://bucket/off.mp4');
    expect(await stateOf('off')).toBe('held');
  });
});

describe('localize', () => {
  async function seedOutbox(awemeId: string, state: string): Promise<void> {
    await seedDownloading(db, awemeId);
    await markDone(db, awemeId, `s3://bucket/${awemeId}.mp4`);
    await pool.query(`UPDATE localization_outbox SET state = $2 WHERE aweme_id = $1`, [awemeId, state]);
  }

  it('moves only held rows, reports why the others were skipped, and logs it', async () => {
    await seedOutbox('held', 'held');
    await seedOutbox('sent', 'dispatched');
    await seedOutbox('bad', 'rejected');

    const result = await localize(db, ['held', 'sent', 'bad', 'ghost', 'held']);

    expect(result.localized).toEqual(['held']);
    expect(result.skipped).toEqual(
      expect.arrayContaining([
        { aweme_id: 'sent', reason: 'not_held', state: 'dispatched' },
        { aweme_id: 'bad', reason: 'not_held', state: 'rejected' },
        { aweme_id: 'ghost', reason: 'no_outbox_row' },
      ]),
    );
    expect(result.skipped).toHaveLength(3);
    expect(await stateOf('held')).toBe('pending');
    expect(await stateOf('sent')).toBe('dispatched');
    expect(await stateOf('bad')).toBe('rejected');

    const { rows: log } = await pool.query(`SELECT aweme_id, action FROM dashboard_actions`);
    expect(log).toEqual([{ aweme_id: 'held', action: 'localize' }]);
  });

  it('is a no-op the second time', async () => {
    await seedOutbox('h', 'held');
    await localize(db, ['h']);
    const again = await localize(db, ['h']);
    expect(again.localized).toEqual([]);
    expect(again.skipped).toEqual([{ aweme_id: 'h', reason: 'not_held', state: 'pending' }]);
    const { rows: log } = await pool.query(`SELECT 1 FROM dashboard_actions`);
    expect(log).toHaveLength(1);
  });

  it('makes the row claimable now, while a held row is not claimable', async () => {
    await seedOutbox('h', 'held');
    await pool.query(`UPDATE localization_outbox SET next_attempt_at = now() + interval '1 hour'`);
    const claimable = `SELECT aweme_id FROM localization_outbox WHERE state = 'pending' AND next_attempt_at <= now()`;
    expect((await pool.query(claimable)).rows).toEqual([]);
    await localize(db, ['h']);
    expect((await pool.query(claimable)).rows).toEqual([{ aweme_id: 'h' }]);
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
