/**
 * The collections migration against a database that already holds channels,
 * videos and jobs: the backfill gives every channel exactly one collection,
 * puts every video in it, and the constraints refuse the values later phases
 * must never store.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Pool, PoolClient } from 'pg';
import {
  FORWARD_MIGRATIONS,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testSchemaName,
} from '../support/db';
import { channelCollection } from '../support/collections';

jest.setTimeout(60_000);

const MIGRATIONS_DIR = resolve(__dirname, '../../migrations');
const UP = readFileSync(resolve(MIGRATIONS_DIR, '261114-collections.sql'), 'utf8');
const DOWN = readFileSync(resolve(MIGRATIONS_DIR, '261114-collections.down.sql'), 'utf8');
const BEFORE = FORWARD_MIGRATIONS.filter((f) => !f.endsWith('261114-collections.sql'));

const SCHEMA = testSchemaName('collections_mig');

let pool: Pool;

async function freshSchemaBeforeCollections(): Promise<void> {
  await pool.end().catch(() => undefined);
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  for (const file of BEFORE) await pool.query(readFileSync(file, 'utf8'));
}

/** Two channels as they exist today: one with a padded nickname, one without. */
async function seedLegacyData(): Promise<void> {
  await pool.query(
    `INSERT INTO profiles (sec_user_id, nickname) VALUES ('sec-a', '  IM-薛珍珠  '), ('sec-b', NULL),
       ('sec-blank', '   ')`,
  );
  await pool.query(
    `INSERT INTO videos (aweme_id, sec_user_id, create_time, status)
     VALUES ('a1', 'sec-a', now(), 'done'), ('a2', 'sec-a', now(), 'pending'),
            ('b1', 'sec-b', now(), 'done')`,
  );
  await pool.query(`INSERT INTO jobs (sec_user_id) VALUES ('sec-a')`);
}

/** A statement expected to fail, on its own client so the pool stays clean. */
async function rejects(statement: string, params: unknown[] = []): Promise<string> {
  const client: PoolClient = await pool.connect();
  try {
    await client.query(statement, params);
  } catch (err) {
    return (err as Error).message;
  } finally {
    // A migration file opens its own transaction; leave no aborted one behind.
    await client.query('ROLLBACK');
    client.release();
  }
  throw new Error(`statement unexpectedly succeeded: ${statement}`);
}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
});

afterAll(async () => {
  await pool.end();
  await dropSchema(SCHEMA);
});

describe('backfill of existing channels', () => {
  beforeAll(async () => {
    await freshSchemaBeforeCollections();
    await seedLegacyData();
    await pool.query(UP);
  });

  it('gives every channel exactly one channel collection named after it', async () => {
    const { rows } = await pool.query(
      `SELECT kind, external_id, sec_user_id, name FROM collections ORDER BY external_id`,
    );
    expect(rows).toEqual([
      { kind: 'channel', external_id: 'sec-a', sec_user_id: 'sec-a', name: 'IM-薛珍珠' },
      { kind: 'channel', external_id: 'sec-b', sec_user_id: 'sec-b', name: 'sec-b' },
      { kind: 'channel', external_id: 'sec-blank', sec_user_id: 'sec-blank', name: 'sec-blank' },
    ]);
  });

  it('puts every existing video in its own channel collection, chosen by nobody', async () => {
    const { rows } = await pool.query(
      `SELECT v.aweme_id, c.external_id, v.mix_episode, v.collection_manual
         FROM videos v JOIN collections c ON c.id = v.collection_id ORDER BY v.aweme_id`,
    );
    expect(rows).toEqual([
      { aweme_id: 'a1', external_id: 'sec-a', mix_episode: null, collection_manual: false },
      { aweme_id: 'a2', external_id: 'sec-a', mix_episode: null, collection_manual: false },
      { aweme_id: 'b1', external_id: 'sec-b', mix_episode: null, collection_manual: false },
    ]);
  });

  it('leaves existing jobs without a hand-picked collection', async () => {
    const { rows } = await pool.query(`SELECT collection_id FROM jobs`);
    expect(rows).toEqual([{ collection_id: null }]);
  });

  it('makes collection_id mandatory from now on', async () => {
    expect(
      await rejects(
        `INSERT INTO videos (aweme_id, sec_user_id, create_time) VALUES ('no-col', 'sec-a', now())`,
      ),
    ).toMatch(/collection_id/);
  });
});

describe('a video whose channel has no profile', () => {
  it('stops the migration with a count instead of guessing a collection', async () => {
    await freshSchemaBeforeCollections();
    await seedLegacyData();
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time) VALUES ('o1', 'gone', now()), ('o2', 'gone', now())`,
    );
    expect(await rejects(UP)).toMatch(/2 videos/);
    const { rows } = await pool.query(
      `SELECT count(*)::int AS n FROM information_schema.tables
        WHERE table_schema = $1 AND table_name = 'collections'`,
      [SCHEMA],
    );
    expect(rows[0].n).toBe(0);
  });
});

describe('constraints', () => {
  let channel: string;

  beforeAll(async () => {
    await freshSchemaBeforeCollections();
    await pool.query(UP);
    channel = await channelCollection(pool, 'sec-c');
  });

  const manual = (column: string, value: unknown) =>
    rejects(`INSERT INTO collections (kind, name, ${column}) VALUES ('manual', 'x', $1)`, [value]);

  it('accepts a fully configured manual collection', async () => {
    await pool.query(
      `INSERT INTO collections (kind, name, bgm_offset_db, caption_text_color, cover_color,
                                cover_opacity_pct, save_subdir)
       VALUES ('manual', 'Phim ngắn', -24, '#ffff00', '#000000', 100, 'Drama/成名的代价')`,
    );
    await pool.query(
      `INSERT INTO collections (kind, name, bgm_offset_db, cover_opacity_pct)
       VALUES ('manual', 'Khác', 23.5, 0)`,
    );
  });

  it.each([
    ['kind', `INSERT INTO collections (kind, external_id, name) VALUES ('playlist', 'x', 'x')`, /collections_kind_check/],
    ['manual with an external id', `INSERT INTO collections (kind, external_id, name) VALUES ('manual', 'x', 'x')`, /collections_external_id_check/],
    ['mix without an external id', `INSERT INTO collections (kind, sec_user_id, name) VALUES ('mix', 'sec-c', 'x')`, /collections_external_id_check/],
    ['empty name', `INSERT INTO collections (kind, name) VALUES ('manual', '')`, /collections_name_check/],
    ['long name', `INSERT INTO collections (kind, name) VALUES ('manual', repeat('x', 201))`, /collections_name_check/],
    ['duplicate channel', `INSERT INTO collections (kind, external_id, sec_user_id, name) VALUES ('channel', 'sec-c', 'sec-c', 'y')`, /collections_kind_external_id_key/],
    ['unknown owner', `INSERT INTO collections (kind, external_id, sec_user_id, name) VALUES ('mix', 'm', 'nobody', 'y')`, /collections_sec_user_id_fkey/],
    // The glossary and its rollback find a channel's collection by both keys.
    ['a channel owned by another channel', `INSERT INTO collections (kind, external_id, sec_user_id, name) VALUES ('channel', 'sec-x', 'sec-c', 'y')`, /collections_owner_check/],
    ['a channel without an owner', `INSERT INTO collections (kind, external_id, name) VALUES ('channel', 'sec-y', 'y')`, /collections_owner_check/],
    ['a 合集 without its channel', `INSERT INTO collections (kind, external_id, name) VALUES ('mix', 'm2', 'y')`, /collections_owner_check/],
  ])('refuses %s', async (_label, statement, constraint) => {
    expect(await rejects(statement)).toMatch(constraint);
  });

  it.each([
    ['bgm_offset_db', 24.5],
    ['bgm_offset_db', -24.5],
    ['bgm_offset_db', 0.25],
    ['caption_text_color', '#FFFF00'],
    ['caption_text_color', 'yellow'],
    ['cover_color', '#00000'],
    ['cover_opacity_pct', 101],
    ['cover_opacity_pct', -1],
    ['save_subdir', '/Users/me/Movies'],
    ['save_subdir', '..'],
    ['save_subdir', 'a/../b'],
    ['save_subdir', 'a/./b'],
    ['save_subdir', 'a//b'],
    ['save_subdir', 'a/'],
    ['save_subdir', 'a\nb'],
    ['save_subdir', 'a\tb'],
    ['save_subdir', ''],
    ['save_subdir', 'x'.repeat(513)],
  ])('refuses %s = %p', async (column, value) => {
    expect(await manual(column, value)).toMatch(/check constraint/);
  });

  it('accepts a save_subdir of exactly 512 characters', async () => {
    await pool.query(`INSERT INTO collections (kind, name, save_subdir) VALUES ('manual', 'x', $1)`, [
      'x'.repeat(512),
    ]);
  });

  it('refuses a mix episode below one', async () => {
    expect(
      await rejects(
        `INSERT INTO videos (aweme_id, sec_user_id, create_time, collection_id, mix_episode)
         VALUES ('e0', 'sec-c', now(), $1, 0)`,
        [channel],
      ),
    ).toMatch(/check constraint/);
  });

  it('refuses a video or job pointing at a collection that does not exist', async () => {
    const missing = '00000000-0000-0000-0000-000000000000';
    expect(
      await rejects(
        `INSERT INTO videos (aweme_id, sec_user_id, create_time, collection_id) VALUES ('m0', 'sec-c', now(), $1)`,
        [missing],
      ),
    ).toMatch(/foreign key/);
    expect(
      await rejects(`INSERT INTO jobs (sec_user_id, collection_id) VALUES ('sec-c', $1)`, [missing]),
    ).toMatch(/foreign key/);
  });

  describe('folder_exports', () => {
    const insert = (overrides: Record<string, unknown> = {}) => {
      const row = {
        aweme_id: '7689288513854595771',
        generation: 1,
        collection_id: channel,
        target_subdir: 'Drama',
        file_stem: 'E01-7689288513854595771',
        final_key: 'output/review/7689288513854595771/g1/final.mp4',
        ...overrides,
      };
      const cols = Object.keys(row);
      return {
        sql: `INSERT INTO folder_exports (${cols.join(', ')})
              VALUES (${cols.map((_, i) => `$${i + 1}`).join(', ')}) RETURNING state, srt_key`,
        params: Object.values(row),
      };
    };

    beforeAll(async () => {
      await pool.query(
        `INSERT INTO videos (aweme_id, sec_user_id, create_time, collection_id) VALUES ('7689288513854595771', 'sec-c', now(), $1)`,
        [channel],
      );
    });

    it('records a pending request with an optional SRT', async () => {
      const { sql, params } = insert();
      expect((await pool.query(sql, params)).rows).toEqual([{ state: 'pending', srt_key: null }]);
      await pool.query(`UPDATE folder_exports SET state = 'done'`);
      const bare = insert({ file_stem: '7689288513854595771' });
      await pool.query(bare.sql, bare.params);
      await pool.query(`UPDATE folder_exports SET state = 'done'`);
    });

    it.each([
      ['an unknown state', { state: 'queued' }, /folder_exports_state_check/],
      ['a file stem with a path', { file_stem: '../E01-fx1' }, /folder_exports_file_stem_check/],
      ['a file stem from Douyin text', { file_stem: 'E01-成名' }, /folder_exports_file_stem_check/],
      ['a one-digit episode', { file_stem: 'E1-7689288513854595771' }, /folder_exports_file_stem_check/],
      ['a file stem of another video', { file_stem: 'E01-123' }, /folder_exports_file_stem_check/],
      ['a bare file stem of another video', { file_stem: '123' }, /folder_exports_file_stem_check/],
      ['an absolute target', { target_subdir: '/tmp' }, /folder_exports_target_subdir_check/],
      ['a target escaping the root', { target_subdir: 'a/../../etc' }, /folder_exports_target_subdir_check/],
      ['a target with a control character', { target_subdir: 'a\rb' }, /folder_exports_target_subdir_check/],
      ['an unknown video', { aweme_id: '999', file_stem: '999' }, /folder_exports_aweme_id_fkey/],
    ])('refuses %s', async (_label, overrides, constraint) => {
      const { sql, params } = insert(overrides);
      expect(await rejects(sql, params)).toMatch(constraint);
    });

    it('keeps one open request per video generation, and allows a new one once it is settled', async () => {
      await pool.query(`DELETE FROM folder_exports`);
      const first = insert();
      await pool.query(first.sql, first.params);
      expect(await rejects(first.sql, first.params)).toMatch(/folder_exports_one_open_idx/);

      await pool.query(`UPDATE folder_exports SET state = 'exporting'`);
      expect(await rejects(first.sql, first.params)).toMatch(/folder_exports_one_open_idx/);

      await pool.query(`UPDATE folder_exports SET state = 'done'`);
      await pool.query(first.sql, first.params);
      const next = insert({ generation: 2 });
      await pool.query(next.sql, next.params);
      const { rows } = await pool.query(
        `SELECT generation, state FROM folder_exports ORDER BY id`,
      );
      expect(rows).toEqual([
        { generation: 1, state: 'done' },
        { generation: 1, state: 'pending' },
        { generation: 2, state: 'pending' },
      ]);
    });
  });
});

describe('rollback', () => {
  it('removes everything the migration added and can be applied again', async () => {
    await freshSchemaBeforeCollections();
    await seedLegacyData();
    await pool.query(UP);
    await pool.query(DOWN);

    const { rows: tables } = await pool.query(
      `SELECT table_name FROM information_schema.tables
        WHERE table_schema = $1 AND table_name IN ('collections', 'folder_exports')`,
      [SCHEMA],
    );
    expect(tables).toEqual([]);
    const { rows: cols } = await pool.query(
      `SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema = $1 AND column_name IN ('collection_id', 'mix_episode', 'collection_manual')`,
      [SCHEMA],
    );
    expect(cols).toEqual([]);
    expect((await pool.query(`SELECT count(*)::int AS n FROM videos`)).rows[0].n).toBe(3);

    await pool.query(UP);
    const { rows } = await pool.query(`SELECT count(*)::int AS n FROM collections`);
    expect(rows[0].n).toBe(3);
  });
});
