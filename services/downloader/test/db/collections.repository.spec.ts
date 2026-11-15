/**
 * Collection assignment at intake: which collection a new video goes to (a
 * collection picked by hand, then its 合集, then its channel), how collections
 * are created and named, and how a video already stored in its channel
 * collection moves into its 合集.
 *
 * Real Postgres, like every repository suite here: `ON CONFLICT` under two
 * concurrent writers is Postgres behaviour, not something a fake can show.
 */
import { drizzle } from 'drizzle-orm/node-postgres';
import type { Pool, PoolClient } from 'pg';
import * as schema from '../../src/db/schema';
import {
  CollectionNotFoundError,
  collectionExists,
  intakeVideos,
  resolveCollections,
  type CollectionHints,
} from '../../src/db/collections.repository';
import type { VideoUpsert } from '../../src/db/videos.repository';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';
import { channelCollection } from '../support/collections';

jest.setTimeout(60_000);

const SCHEMA = testSchemaName('collections_repo');
const CHANNEL = 'MS4wLjABAAAA_collections_channel';
const MIX_A = '7695338683825653823';
const MIX_B = '7600000000000000001';
const TTL = { urlTtlSeconds: 7200 };

let pool: Pool;
let db: ReturnType<typeof testDb>;

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA, { max: 6 });
  await applyForwardMigration(pool);
  db = testDb(pool);
});

afterAll(async () => {
  await pool.end();
  await dropSchema(SCHEMA);
});

beforeEach(async () => {
  await pool.query(`INSERT INTO profiles (sec_user_id) VALUES ($1)`, [CHANNEL]);
});

afterEach(async () => {
  await truncateAll(pool);
});

const mix = (mixId: string, episode: number | null, mixName = '成名的代价') => ({
  mixId,
  mixName,
  episode,
});

function row(
  awemeId: string,
  hints: Partial<CollectionHints> = {},
): VideoUpsert & CollectionHints {
  return {
    awemeId,
    secUserId: CHANNEL,
    desc: null,
    createTime: new Date(Date.UTC(2026, 0, 2)),
    durationMs: 10_000,
    playAddr: `https://cdn.example/${awemeId}.mp4`,
    mix: null,
    authorNickname: 'IM-薛珍珠',
    ...hints,
  };
}

interface CollectionRow {
  id: string;
  kind: string;
  external_id: string | null;
  sec_user_id: string | null;
  name: string;
}

async function allCollections(): Promise<CollectionRow[]> {
  const { rows } = await pool.query<CollectionRow>(
    `SELECT id, kind, external_id, sec_user_id, name FROM collections ORDER BY kind, external_id`,
  );
  return rows;
}

interface Placement {
  kind: string;
  external_id: string | null;
  name: string;
  mix_episode: number | null;
  collection_manual: boolean;
}

async function placement(awemeId: string): Promise<Placement> {
  const { rows } = await pool.query<Placement>(
    `SELECT c.kind, c.external_id, c.name, v.mix_episode, v.collection_manual
       FROM videos v JOIN collections c ON c.id = v.collection_id
      WHERE v.aweme_id = $1`,
    [awemeId],
  );
  if (rows.length !== 1) throw new Error(`video ${awemeId} not found`);
  return rows[0]!;
}

/** A manual collection, as phase 4's API will create it. */
async function manualCollection(name: string): Promise<string> {
  const { rows } = await pool.query<{ id: string }>(
    `INSERT INTO collections (kind, external_id, sec_user_id, name) VALUES ('manual', NULL, NULL, $1)
     RETURNING id`,
    [name],
  );
  return rows[0]!.id;
}

describe('resolveCollections', () => {
  it('puts a 合集 item in its 合集 with its episode and anything else in the channel collection', async () => {
    const resolved = await resolveCollections(db, {
      secUserId: CHANNEL,
      channelName: 'IM-薛珍珠',
      manualCollectionId: null,
      items: [{ mix: mix(MIX_A, 3) }, { mix: null }],
    });

    const collections = await allCollections();
    const channel = collections.find((c) => c.kind === 'channel')!;
    const mixRow = collections.find((c) => c.kind === 'mix')!;
    expect(collections).toHaveLength(2);
    expect(channel).toMatchObject({ external_id: CHANNEL, sec_user_id: CHANNEL, name: 'IM-薛珍珠' });
    expect(mixRow).toMatchObject({ external_id: MIX_A, sec_user_id: CHANNEL, name: '成名的代价' });
    expect(resolved.channelCollectionId).toBe(channel.id);
    expect(resolved.assignments).toEqual([
      { collectionId: mixRow.id, mixEpisode: 3, collectionManual: false },
      { collectionId: channel.id, mixEpisode: null, collectionManual: false },
    ]);
  });

  it('creates nothing new when it runs again over the same items', async () => {
    const input = {
      secUserId: CHANNEL,
      channelName: 'IM-薛珍珠',
      manualCollectionId: null,
      items: [{ mix: mix(MIX_A, 1) }, { mix: mix(MIX_B, 2, '另一个合集') }, { mix: null }],
    };
    const first = await resolveCollections(db, input);
    const second = await resolveCollections(db, input);

    expect(await allCollections()).toHaveLength(3);
    expect(second).toEqual(first);
  });

  it('sends every item to a collection picked by hand, marked manual', async () => {
    const picked = await manualCollection('Chọn tay');

    const resolved = await resolveCollections(db, {
      secUserId: CHANNEL,
      channelName: 'IM-薛珍珠',
      manualCollectionId: picked,
      items: [{ mix: mix(MIX_A, 4) }, { mix: null }],
    });

    expect(resolved.assignments).toEqual([
      { collectionId: picked, mixEpisode: 4, collectionManual: true },
      { collectionId: picked, mixEpisode: null, collectionManual: true },
    ]);
    // The 合集 still exists, so a video already stored can move into it.
    expect((await allCollections()).filter((c) => c.kind === 'mix')).toHaveLength(1);
  });

  it('still creates the channel collection when every item goes to a hand-picked one', async () => {
    // The glossary of a channel's runs is keyed to its channel collection
    // until run settings land, so intake must not stop creating it.
    const picked = await manualCollection('Chọn tay');

    const resolved = await resolveCollections(db, {
      secUserId: CHANNEL,
      channelName: 'IM-薛珍珠',
      manualCollectionId: picked,
      items: [{ mix: null }],
    });

    const channel = (await allCollections()).find((c) => c.kind === 'channel');
    expect(channel).toMatchObject({ external_id: CHANNEL, name: 'IM-薛珍珠' });
    expect(resolved.channelCollectionId).toBe(channel!.id);
  });

  it('refuses a hand-picked collection that does not exist and creates nothing', async () => {
    const missing = '00000000-0000-4000-8000-000000000000';

    await expect(
      resolveCollections(db, {
        secUserId: CHANNEL,
        channelName: 'IM-薛珍珠',
        manualCollectionId: missing,
        items: [{ mix: mix(MIX_A, 1) }],
      }),
    ).rejects.toBeInstanceOf(CollectionNotFoundError);
    expect(await allCollections()).toEqual([]);
  });

  it('refuses a channel without a profile instead of inventing an owner', async () => {
    await expect(
      resolveCollections(db, {
        secUserId: 'no-such-profile',
        channelName: null,
        manualCollectionId: null,
        items: [{ mix: null }],
      }),
    ).rejects.toThrow(/no-such-profile/);
  });

  describe('names', () => {
    it('renames a channel collection still named after its sec_user_id to the nickname', async () => {
      await channelCollection(pool, CHANNEL); // named CHANNEL, like the backfill without a nickname

      await resolveCollections(db, {
        secUserId: CHANNEL,
        channelName: 'IM-薛珍珠',
        manualCollectionId: null,
        items: [{ mix: null }],
      });

      expect((await allCollections())[0]!.name).toBe('IM-薛珍珠');
    });

    it('keeps a channel collection the user has renamed', async () => {
      const id = await channelCollection(pool, CHANNEL);
      await pool.query(`UPDATE collections SET name = 'Tên tôi đặt' WHERE id = $1`, [id]);

      await resolveCollections(db, {
        secUserId: CHANNEL,
        channelName: 'IM-薛珍珠',
        manualCollectionId: null,
        items: [{ mix: null }],
      });

      expect((await allCollections())[0]!.name).toBe('Tên tôi đặt');
    });

    it('renames a 合集 still named after its mix_id once Douyin sends its name', async () => {
      await resolveCollections(db, {
        secUserId: CHANNEL,
        channelName: null,
        manualCollectionId: null,
        // The mapper falls back to the id when Douyin sent no usable name.
        items: [{ mix: mix(MIX_A, 1, MIX_A) }],
      });
      expect((await allCollections()).find((c) => c.kind === 'mix')!.name).toBe(MIX_A);

      await resolveCollections(db, {
        secUserId: CHANNEL,
        channelName: null,
        manualCollectionId: null,
        items: [{ mix: mix(MIX_A, 1) }],
      });

      expect((await allCollections()).find((c) => c.kind === 'mix')!.name).toBe('成名的代价');
    });

    it('keeps a 合集 the user has renamed', async () => {
      await resolveCollections(db, {
        secUserId: CHANNEL,
        channelName: null,
        manualCollectionId: null,
        items: [{ mix: mix(MIX_A, 1) }],
      });
      await pool.query(`UPDATE collections SET name = 'Đổi tên' WHERE kind = 'mix'`);

      await resolveCollections(db, {
        secUserId: CHANNEL,
        channelName: null,
        manualCollectionId: null,
        items: [{ mix: mix(MIX_A, 1, '新名字') }],
      });

      expect((await allCollections()).find((c) => c.kind === 'mix')!.name).toBe('Đổi tên');
    });

    it('writes the nickname into a profile that has none, and leaves one that has', async () => {
      await pool.query(`INSERT INTO profiles (sec_user_id, nickname) VALUES ('named', 'Tên cũ')`);

      await resolveCollections(db, {
        secUserId: CHANNEL,
        channelName: 'IM-薛珍珠',
        manualCollectionId: null,
        items: [{ mix: null }],
      });
      await resolveCollections(db, {
        secUserId: 'named',
        channelName: 'Tên mới',
        manualCollectionId: null,
        items: [{ mix: null }],
      });

      const { rows } = await pool.query(
        `SELECT sec_user_id, nickname FROM profiles ORDER BY sec_user_id`,
      );
      expect(rows).toEqual([
        { sec_user_id: CHANNEL, nickname: 'IM-薛珍珠' },
        { sec_user_id: 'named', nickname: 'Tên cũ' },
      ]);
    });

    it('names a new channel collection after the stored nickname when the items carry none', async () => {
      await pool.query(`UPDATE profiles SET nickname = '  Đã lưu  ' WHERE sec_user_id = $1`, [CHANNEL]);

      await resolveCollections(db, {
        secUserId: CHANNEL,
        channelName: null,
        manualCollectionId: null,
        items: [{ mix: null }],
      });

      expect((await allCollections())[0]!.name).toBe('Đã lưu');
    });
  });

  it('gives two overlapping writers of the same 合集 one row', async () => {
    // Writer A creates the 合集 inside a transaction it holds open. Writer B
    // then asks for the same 合集 on its own connection. B's insert must wait
    // on A's uncommitted row -- the test checks Postgres reports B as waiting
    // on a lock, so the two statements really overlap -- and once A commits,
    // B must answer with A's row instead of failing or creating a second one.
    await channelCollection(pool, CHANNEL);
    await pool.query(
      `UPDATE collections SET name = 'IM-薛珍珠' WHERE kind = 'channel';
       UPDATE profiles SET nickname = 'IM-薛珍珠' WHERE sec_user_id = '${CHANNEL}'`,
    );
    const input = {
      secUserId: CHANNEL,
      channelName: 'IM-薛珍珠',
      manualCollectionId: null,
      items: [{ mix: mix(MIX_A, 1) }],
    };

    const clientA: PoolClient = await pool.connect();
    const clientB: PoolClient = await pool.connect();
    try {
      const pidB = (await clientB.query<{ pid: number }>('SELECT pg_backend_pid() AS pid')).rows[0]!.pid;

      await clientA.query('BEGIN');
      const a = await resolveCollections(drizzle(clientA, { schema }), input);

      let settled = false;
      const bPromise = resolveCollections(drizzle(clientB, { schema }), input).finally(() => {
        settled = true;
      });

      await waitUntil(async () => {
        const { rows } = await pool.query<{ wait_event_type: string | null }>(
          `SELECT wait_event_type FROM pg_stat_activity WHERE pid = $1`,
          [pidB],
        );
        return rows[0]?.wait_event_type === 'Lock';
      }, 'writer B never waited on writer A');
      expect(settled).toBe(false);

      await clientA.query('COMMIT');
      const b = await bPromise;

      expect(b.assignments[0]!.collectionId).toBe(a.assignments[0]!.collectionId);
      expect((await allCollections()).filter((c) => c.kind === 'mix')).toHaveLength(1);
    } finally {
      await clientA.query('ROLLBACK').catch(() => undefined);
      clientA.release();
      clientB.release();
    }
  });
});

describe('intakeVideos', () => {
  it('stores new videos in their 合集 with episodes and the rest in the channel collection', async () => {
    const inserted = await intakeVideos(
      db,
      {
        secUserId: CHANNEL,
        manualCollectionId: null,
        rows: [row('e1', { mix: mix(MIX_A, 1) }), row('e2', { mix: mix(MIX_A, 2) }), row('solo')],
      },
      TTL,
    );

    expect(inserted).toBe(3);
    expect(await placement('e1')).toMatchObject({ kind: 'mix', mix_episode: 1, collection_manual: false });
    expect(await placement('e2')).toMatchObject({ kind: 'mix', mix_episode: 2 });
    expect(await placement('solo')).toMatchObject({
      kind: 'channel',
      name: 'IM-薛珍珠',
      mix_episode: null,
      collection_manual: false,
    });
  });

  it('marks every new video manual when a collection was picked by hand', async () => {
    const picked = await manualCollection('Chọn tay');

    await intakeVideos(
      db,
      { secUserId: CHANNEL, manualCollectionId: picked, rows: [row('m1', { mix: mix(MIX_A, 5) }), row('m2')] },
      TTL,
    );

    expect(await placement('m1')).toMatchObject({ kind: 'manual', mix_episode: 5, collection_manual: true });
    expect(await placement('m2')).toMatchObject({ kind: 'manual', mix_episode: null, collection_manual: true });
  });

  it('does not take a nickname that is only the sec_user_id placeholder as the channel name', async () => {
    // The mapper returns the sec_user_id when a nickname cleans to nothing.
    await intakeVideos(
      db,
      { secUserId: CHANNEL, manualCollectionId: null, rows: [row('p1', { authorNickname: CHANNEL })] },
      TTL,
    );

    const { rows } = await pool.query(`SELECT nickname FROM profiles WHERE sec_user_id = $1`, [CHANNEL]);
    expect(rows).toEqual([{ nickname: null }]);
  });

  it('names a new 合集 after the first real name on the page, not an id placeholder before it', async () => {
    await intakeVideos(
      db,
      {
        secUserId: CHANNEL,
        manualCollectionId: null,
        rows: [row('n1', { mix: mix(MIX_A, 1, MIX_A) }), row('n2', { mix: mix(MIX_A, 2) })],
      },
      TTL,
    );

    expect((await placement('n1')).name).toBe('成名的代价');
  });

  it('does nothing for an empty page', async () => {
    await expect(
      intakeVideos(db, { secUserId: CHANNEL, manualCollectionId: null, rows: [] }, TTL),
    ).resolves.toBe(0);
    expect(await allCollections()).toEqual([]);
  });

  describe('a video already stored', () => {
    async function storeInChannel(awemeId: string, extra = ''): Promise<void> {
      const channelId = await channelCollection(pool, CHANNEL);
      await pool.query(
        `INSERT INTO videos (aweme_id, sec_user_id, create_time, collection_id ${extra ? ', collection_manual' : ''})
         VALUES ($1, $2, now(), $3 ${extra ? ', true' : ''})`,
        [awemeId, CHANNEL, channelId],
      );
    }

    it('moves from the channel collection into its 合集 with its episode', async () => {
      await storeInChannel('old');

      const inserted = await intakeVideos(
        db,
        { secUserId: CHANNEL, manualCollectionId: null, rows: [row('old', { mix: mix(MIX_A, 7) })] },
        TTL,
      );

      expect(inserted).toBe(0);
      expect(await placement('old')).toMatchObject({
        kind: 'mix',
        external_id: MIX_A,
        mix_episode: 7,
        collection_manual: false,
      });
    });

    it('stays where a person put it', async () => {
      await storeInChannel('chosen', 'manual');

      await intakeVideos(
        db,
        { secUserId: CHANNEL, manualCollectionId: null, rows: [row('chosen', { mix: mix(MIX_A, 7) })] },
        TTL,
      );

      expect(await placement('chosen')).toMatchObject({
        kind: 'channel',
        mix_episode: null,
        collection_manual: true,
      });
    });

    it('stays in a collection other than its channel collection', async () => {
      await intakeVideos(
        db,
        { secUserId: CHANNEL, manualCollectionId: null, rows: [row('elsewhere', { mix: mix(MIX_B, 2, '另一个合集') })] },
        TTL,
      );

      await intakeVideos(
        db,
        { secUserId: CHANNEL, manualCollectionId: null, rows: [row('elsewhere', { mix: mix(MIX_A, 9) })] },
        TTL,
      );

      expect(await placement('elsewhere')).toMatchObject({ external_id: MIX_B, mix_episode: 2 });
    });

    it('gets its episode filled in when it is already in the 合集 without one', async () => {
      await intakeVideos(
        db,
        { secUserId: CHANNEL, manualCollectionId: null, rows: [row('no-ep', { mix: mix(MIX_A, null) })] },
        TTL,
      );
      expect((await placement('no-ep')).mix_episode).toBeNull();

      await intakeVideos(
        db,
        { secUserId: CHANNEL, manualCollectionId: null, rows: [row('no-ep', { mix: mix(MIX_A, 6) })] },
        TTL,
      );

      expect(await placement('no-ep')).toMatchObject({ kind: 'mix', mix_episode: 6 });
    });

    it('keeps an episode it already has', async () => {
      await intakeVideos(
        db,
        { secUserId: CHANNEL, manualCollectionId: null, rows: [row('has-ep', { mix: mix(MIX_A, 3) })] },
        TTL,
      );

      await intakeVideos(
        db,
        { secUserId: CHANNEL, manualCollectionId: null, rows: [row('has-ep', { mix: mix(MIX_A, 4) })] },
        TTL,
      );

      expect((await placement('has-ep')).mix_episode).toBe(3);
    });

    it('still moves into its 合集 when the job picked a collection by hand (the pick is for new videos)', async () => {
      await storeInChannel('kept');
      const picked = await manualCollection('Chọn tay');

      await intakeVideos(
        db,
        { secUserId: CHANNEL, manualCollectionId: picked, rows: [row('kept', { mix: mix(MIX_A, 1) })] },
        TTL,
      );

      expect(await placement('kept')).toMatchObject({
        kind: 'mix',
        external_id: MIX_A,
        mix_episode: 1,
        collection_manual: false,
      });
    });
  });
});

describe('collectionExists', () => {
  it('answers whether a collection id names a row', async () => {
    const picked = await manualCollection('Có');
    await expect(collectionExists(db, picked)).resolves.toBe(true);
    await expect(collectionExists(db, '00000000-0000-4000-8000-000000000000')).resolves.toBe(false);
  });
});

async function waitUntil(check: () => Promise<boolean>, message: string): Promise<void> {
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error(message);
}
