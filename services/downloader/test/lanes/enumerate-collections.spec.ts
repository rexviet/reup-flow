/**
 * The enumerate lane assigning collections, driven with the real enumerate page
 * of channel IM-薛珍珠 (ten episodes of the 合集 成名的代价 and one video
 * outside it) through `runOnce()` against a real Postgres.
 */
import type { Pool } from 'pg';
import { jobs, profiles } from '../../src/db/schema';
import { EnumerateLane, type EnumerateParser } from '../../src/lanes/enumerate.lane';
import type { UserPostPage, UserProfile } from '../../src/parser/parser.client';
import {
  MIX_PAGE_CHANNEL,
  MIX_PAGE_EPISODE_1_ID,
  MIX_PAGE_MIX_ID,
  MIX_PAGE_NO_MIX_ID,
  mixPageItems,
} from '../support/aweme-fixtures';
import { channelCollection } from '../support/collections';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';

jest.setTimeout(60_000);

const SCHEMA = testSchemaName('enumerate_collections');

let pool: Pool;
let db: ReturnType<typeof testDb>;

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);
});

afterAll(async () => {
  await pool.end();
  await dropSchema(SCHEMA);
});

beforeEach(async () => {
  await db.insert(profiles).values({ secUserId: MIX_PAGE_CHANNEL });
});

afterEach(async () => {
  await truncateAll(pool);
});

/** Answers every page request with the real fixture page, the profile's last. */
class FixturePage implements EnumerateParser {
  async fetchUserPostPage(): Promise<UserPostPage> {
    return { awemeList: mixPageItems(), hasMore: false, maxCursor: 1_700_000_000_000, statusCode: 0 };
  }

  async fetchUserProfile(): Promise<UserProfile> {
    throw new Error('the F4 probe must not run on a page that succeeded');
  }
}

const silent = { error: () => undefined, warn: () => undefined, log: () => undefined };

async function enumerateOnce(collectionId: string | null = null): Promise<void> {
  await db.insert(jobs).values({ secUserId: MIX_PAGE_CHANNEL, collectionId });
  const lane = new EnumerateLane(silent, db, new FixturePage(), {
    leaseSeconds: 300,
    urlTtlSeconds: 7200,
    sleepMsMin: 0,
    sleepMsMax: 0,
    probeSecUserId: 'MS4wLjABAAAA_probe_target',
  });
  await expect(lane.runOnce(new AbortController().signal)).resolves.toBe('worked');
  const { rows } = await pool.query<{ status: string }>(`SELECT status FROM jobs ORDER BY created_at DESC LIMIT 1`);
  expect(rows[0]!.status).toBe('downloading');
}

interface Placed {
  aweme_id: string;
  kind: string;
  name: string;
  mix_episode: number | null;
  collection_manual: boolean;
}

async function placements(): Promise<Placed[]> {
  const { rows } = await pool.query<Placed>(
    `SELECT v.aweme_id, c.kind, c.name, v.mix_episode, v.collection_manual
       FROM videos v JOIN collections c ON c.id = v.collection_id
      ORDER BY v.mix_episode NULLS LAST, v.aweme_id`,
  );
  return rows;
}

async function collectionCount(): Promise<number> {
  const { rows } = await pool.query<{ n: number }>(`SELECT count(*)::int AS n FROM collections`);
  return rows[0]!.n;
}

it('puts the ten episodes in the 合集 成名的代价 as episodes 1-10 and the other video in the channel collection', async () => {
  await enumerateOnce();

  const placed = await placements();
  const inMix = placed.filter((p) => p.kind === 'mix');
  expect(placed).toHaveLength(11);
  expect(inMix.map((p) => p.mix_episode)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  expect(new Set(inMix.map((p) => p.name))).toEqual(new Set(['成名的代价']));
  expect(placed.filter((p) => p.kind === 'channel')).toEqual([
    {
      aweme_id: MIX_PAGE_NO_MIX_ID,
      kind: 'channel',
      name: 'IM-薛珍珠',
      mix_episode: null,
      collection_manual: false,
    },
  ]);
  const { rows } = await pool.query(`SELECT external_id FROM collections WHERE kind = 'mix'`);
  expect(rows).toEqual([{ external_id: MIX_PAGE_MIX_ID }]);
});

it('creates no new collection when the channel is enumerated again', async () => {
  await enumerateOnce();
  const before = await collectionCount();

  await enumerateOnce();

  expect(before).toBe(2);
  expect(await collectionCount()).toBe(2);
  expect((await placements()).filter((p) => p.kind === 'mix')).toHaveLength(10);
});

it('moves videos stored in the channel collection (as after the backfill) into the 合集, except one a person placed', async () => {
  const channelId = await channelCollection(pool, MIX_PAGE_CHANNEL);
  const stored = mixPageItems().map((item) => String(item.aweme_id));
  for (const awemeId of stored) {
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, collection_id, collection_manual)
       VALUES ($1, $2, now(), $3, $4)`,
      [awemeId, MIX_PAGE_CHANNEL, channelId, awemeId === MIX_PAGE_EPISODE_1_ID],
    );
  }

  await enumerateOnce();

  const placed = await placements();
  expect(placed.filter((p) => p.kind === 'mix').map((p) => p.mix_episode)).toEqual([
    2, 3, 4, 5, 6, 7, 8, 9, 10,
  ]);
  expect(placed.find((p) => p.aweme_id === MIX_PAGE_EPISODE_1_ID)).toMatchObject({
    kind: 'channel',
    mix_episode: null,
    collection_manual: true,
  });
  expect(placed.find((p) => p.aweme_id === MIX_PAGE_NO_MIX_ID)).toMatchObject({ kind: 'channel' });
  // The channel collection was named after its id by the backfill; the page names it.
  expect(placed.find((p) => p.aweme_id === MIX_PAGE_NO_MIX_ID)!.name).toBe('IM-薛珍珠');
});

it('puts every new video of a job with a hand-picked collection there, marked manual', async () => {
  const { rows } = await pool.query<{ id: string }>(
    `INSERT INTO collections (kind, name) VALUES ('manual', 'Chọn tay') RETURNING id`,
  );

  await enumerateOnce(rows[0]!.id);

  const placed = await placements();
  expect(placed).toHaveLength(11);
  for (const p of placed) expect(p).toMatchObject({ kind: 'manual', name: 'Chọn tay', collection_manual: true });
});

it('still moves backfilled episodes into the 合集 when the job picked a collection by hand', async () => {
  const channelId = await channelCollection(pool, MIX_PAGE_CHANNEL);
  for (const item of mixPageItems()) {
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, collection_id) VALUES ($1, $2, now(), $3)`,
      [String(item.aweme_id), MIX_PAGE_CHANNEL, channelId],
    );
  }
  const { rows } = await pool.query<{ id: string }>(
    `INSERT INTO collections (kind, name) VALUES ('manual', 'Chọn tay') RETURNING id`,
  );

  await enumerateOnce(rows[0]!.id);

  const placed = await placements();
  expect(placed.filter((p) => p.kind === 'mix').map((p) => p.mix_episode)).toEqual([
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
  ]);
  expect(placed.find((p) => p.aweme_id === MIX_PAGE_NO_MIX_ID)).toMatchObject({ kind: 'channel' });
});
