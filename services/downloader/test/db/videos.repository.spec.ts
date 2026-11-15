/**
 * Phase 1 -- `videos` claim/reaper repository (phase-01 §1.3, DoD 2-5).
 *
 * Everything here runs against a real Postgres. That is not a preference:
 * SKIP LOCKED semantics, lease expiry against the database clock, and
 * `attempts` arithmetic inside a single statement have no meaningful
 * in-memory equivalent, and a fake would prove only that the fake agrees
 * with itself.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import {
  claimOne,
  countByStatusForProfile,
  upsertMany,
  listByProfile,
  markDone,
  markFailed,
  reapOrphans,
  release,
  resetFailedForProfile,
} from '../../src/db/videos.repository';
import { videos, type Video } from '../../src/db/schema';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';
import { assertGenuineOverlap, runConcurrentlyHoldingLocks } from '../support/concurrency-harness';
import { channelCollection } from '../support/collections';

jest.setTimeout(60_000);

const SCHEMA = testSchemaName('videos');
const LEASE_SECONDS = 300;
const MAX_ATTEMPTS = 3;
const OPTS = { leaseSeconds: LEASE_SECONDS, maxAttempts: MAX_ATTEMPTS };

/**
 * Same reasoning as the phase-0 spike: tight enough that a claimer genuinely
 * blocked on a missing SKIP LOCKED hits it (the winners hold their locks
 * open until every contender settles, so a blocked one has nothing to wake
 * up for), comfortably above the few ms an unblocked claim takes.
 */
const CONCURRENCY_STATEMENT_TIMEOUT_MS = 500;

let pool: Pool;
let db: ReturnType<typeof testDb>;
/** Channel collections of the two profiles every test here uses. */
let collectionOf: Record<'profile-1' | 'profile-2', string>;

/** Seeds `count` pending videos, oldest first, ids v0..v{count-1}. */
async function seedPending(count: number): Promise<void> {
  await db.insert(videos).values(
    Array.from({ length: count }, (_, i) => ({
      awemeId: `v${i}`,
      secUserId: 'profile-1',
      collectionId: collectionOf['profile-1'],
      createTime: new Date(Date.UTC(2026, 0, 1) + i * 60_000),
    })),
  );
}

async function readVideo(awemeId: string): Promise<Video> {
  const [row] = await db.select().from(videos).where(eq(videos.awemeId, awemeId));
  if (!row) throw new Error(`video ${awemeId} not found`);
  return row;
}

/**
 * Simulates "the worker died and the lease elapsed" by moving the lease into
 * the past, rather than by sleeping through a real one. The reaper compares
 * against the database clock either way, so this exercises the real
 * predicate -- it just doesn't take five minutes.
 */
async function expireLease(awemeId: string): Promise<void> {
  await pool.query(
    "UPDATE videos SET lease_expires_at = now() - interval '1 hour' WHERE aweme_id = $1",
    [awemeId],
  );
}

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
  collectionOf = {
    'profile-1': await channelCollection(pool, 'profile-1'),
    'profile-2': await channelCollection(pool, 'profile-2'),
  };
});

afterEach(async () => {
  await truncateAll(pool);
});

describe('claimOne', () => {
  it('claims the oldest pending video and puts it under lease', async () => {
    await seedPending(3);

    const claimed = await claimOne(db, OPTS);

    expect(claimed).not.toBeNull();
    expect(claimed!.awemeId).toBe('v0');
    expect(claimed!.status).toBe('downloading');
    expect(claimed!.leaseExpiresAt).toBeInstanceOf(Date);
    // Lease is computed from the DATABASE clock, so assert a window rather
    // than an exact value -- the host clock is not the reference here.
    const deltaMs = claimed!.leaseExpiresAt!.getTime() - Date.now();
    expect(deltaMs).toBeGreaterThan((LEASE_SECONDS - 60) * 1000);
    expect(deltaMs).toBeLessThan((LEASE_SECONDS + 60) * 1000);
  });

  it('returns null when there is nothing pending', async () => {
    expect(await claimOne(db, OPTS)).toBeNull();
  });

  it('increments attempts at CLAIM time, not at failure time (F6)', async () => {
    await seedPending(1);

    const claimed = await claimOne(db, OPTS);

    // The whole point of F6: a worker that crashes here -- before it can
    // report anything -- has still burned an attempt. If attempts were
    // incremented on failure, a crash-looping video would be re-claimed
    // forever. See the crash-loop test below for the end-to-end version.
    expect(claimed!.attempts).toBe(1);
    expect((await readVideo('v0')).attempts).toBe(1);
  });

  it('never claims a video that has already used all its attempts', async () => {
    await seedPending(1);
    await pool.query('UPDATE videos SET attempts = $1', [MAX_ATTEMPTS]);

    expect(await claimOne(db, OPTS)).toBeNull();
  });

  it('leaves a video claimed by someone else alone', async () => {
    await seedPending(2);
    const first = await claimOne(db, OPTS);
    const second = await claimOne(db, OPTS);

    expect(first!.awemeId).toBe('v0');
    expect(second!.awemeId).toBe('v1');
    expect(await claimOne(db, OPTS)).toBeNull();
  });

  it('N=10 genuinely concurrent claimers claim all 10 rows, none twice', async () => {
    const N = 10;
    const claimerCount = N + 2;
    await seedPending(N);

    const pools = Array.from({ length: claimerCount }, () =>
      createTestPool(SCHEMA, { statementTimeoutMs: CONCURRENCY_STATEMENT_TIMEOUT_MS, max: 1 }),
    );
    try {
      // Pre-warm every socket so the barrier is decided by the claim query,
      // not by connection setup.
      await Promise.all(pools.map((p) => p.query('SELECT 1')));
      const dbs = pools.map((p) => testDb(p));

      // Promise.all over transactions is NOT concurrency -- phase 0 proved
      // it can run fully serial while still passing. The harness forces
      // every claimer to open its transaction and park before any of them
      // claims, and forces winners to hold their locks until every
      // contender has settled, which is what makes a missing SKIP LOCKED
      // observable instead of quietly self-healing.
      const { outcomes, intervals } = await runConcurrentlyHoldingLocks(dbs, (tx) =>
        claimOne(tx, OPTS),
      );
      assertGenuineOverlap(intervals);

      const errors = outcomes.filter((o) => o.status === 'rejected').map((o) => o.error);
      expect(errors).toHaveLength(0);

      const claimed = outcomes
        .map((o) => (o.status === 'fulfilled' ? o.value : null))
        .filter((v): v is Video => v !== null);
      const ids = claimed.map((v) => v.awemeId);

      expect(ids).toHaveLength(N);
      expect(new Set(ids).size).toBe(N);
      expect(new Set(ids)).toEqual(new Set(Array.from({ length: N }, (_, i) => `v${i}`)));
      // Every claim burned exactly one attempt -- no row was claimed twice
      // and then "healed" by a second increment.
      const { rows } = await pool.query<{ attempts: number }>(
        'SELECT attempts FROM videos ORDER BY aweme_id',
      );
      expect(rows.map((r) => Number(r.attempts))).toEqual(Array(N).fill(1));
    } finally {
      await Promise.all(pools.map((p) => p.end()));
    }
  });
});

describe('finishing a claim', () => {
  it('markDone records the storage path and releases the lease', async () => {
    await seedPending(1);
    await claimOne(db, OPTS);

    await markDone(db, 'v0', '/data/profile-1/v0.mp4');

    const row = await readVideo('v0');
    expect(row.status).toBe('done');
    expect(row.storagePath).toBe('/data/profile-1/v0.mp4');
    expect(row.leaseExpiresAt).toBeNull();
    expect(await claimOne(db, OPTS)).toBeNull();
  });

  it('markFailed hands a video back for retry while attempts remain', async () => {
    await seedPending(1);
    await claimOne(db, OPTS);

    await markFailed(db, 'v0', 'connection reset', OPTS);

    const row = await readVideo('v0');
    expect(row.status).toBe('pending');
    expect(row.lastError).toBe('connection reset');
    expect(row.leaseExpiresAt).toBeNull();
    expect(row.attempts).toBe(1);
    expect((await claimOne(db, OPTS))!.attempts).toBe(2);
  });

  it('markFailed gives up once the attempts are spent', async () => {
    await seedPending(1);
    await pool.query('UPDATE videos SET attempts = $1', [MAX_ATTEMPTS - 1]);
    await claimOne(db, OPTS);

    await markFailed(db, 'v0', 'connection reset', OPTS);

    expect((await readVideo('v0')).status).toBe('failed');
    expect(await claimOne(db, OPTS)).toBeNull();
  });

  // FIX 3 (review): phase-3's own risk register names this exact leak --
  // "cookie vào error của job rồi ra API response" -- and nothing redacted
  // the WRITE path. `error` here stands in for attacker/Douyin-influenced
  // text a later phase (6/7) will pass through unchecked (an upstream error
  // body, a rejected-request message); redacting at write time means the
  // secret never sits in the database at all, so an operator running `psql`
  // -- or a future export/backup -- can never see it either. See the
  // matching read-path test on `toVideoResponse` for why BOTH sides matter.
  it('markFailed redacts a cookie-shaped error before it reaches the row', async () => {
    await seedPending(1);
    await claimOne(db, OPTS);
    const leaked = 'upstream said: sessionid=sentinel-fake-leaked-in-error-0123456789';

    await markFailed(db, 'v0', leaked, OPTS);

    const row = await readVideo('v0');
    expect(row.lastError).not.toContain('sentinel-fake-leaked-in-error-0123456789');
    expect(row.lastError).toContain('sessionid=[redacted]');
  });

  it('release hands work back without recording an error', async () => {
    await seedPending(1);
    await claimOne(db, OPTS);

    await release(db, 'v0', OPTS);

    const row = await readVideo('v0');
    expect(row.status).toBe('pending');
    expect(row.lastError).toBeNull();
    expect(row.leaseExpiresAt).toBeNull();
  });

  it('never leaves a video pending but unclaimable', async () => {
    // A row that is `pending` with no attempts left is invisible to the
    // claim query forever, but still counts as outstanding work in every
    // progress query. Both give-back paths must land on `failed` instead.
    await seedPending(1);
    await pool.query('UPDATE videos SET attempts = $1', [MAX_ATTEMPTS - 1]);
    await claimOne(db, OPTS);

    await release(db, 'v0', OPTS);

    expect((await readVideo('v0')).status).toBe('failed');
  });
});

describe('reapOrphans (F3)', () => {
  it('hands back a video whose worker died, leaving the lease to expire', async () => {
    await seedPending(1);
    await claimOne(db, OPTS);
    await expireLease('v0');

    const reaped = await reapOrphans(db, OPTS);

    expect(reaped.map((v) => v.awemeId)).toEqual(['v0']);
    expect((await readVideo('v0')).status).toBe('pending');
    expect((await readVideo('v0')).leaseExpiresAt).toBeNull();
    expect((await claimOne(db, OPTS))!.attempts).toBe(2);
  });

  it('does not steal work from a worker whose lease is still live', async () => {
    await seedPending(1);
    await claimOne(db, OPTS);

    expect(await reapOrphans(db, OPTS)).toEqual([]);
    expect((await readVideo('v0')).status).toBe('downloading');
  });

  it('crash-loops at most MAX_ATTEMPTS times, then gives up (F6)', async () => {
    await seedPending(1);

    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
      const claimed = await claimOne(db, OPTS);
      expect(claimed).not.toBeNull();
      // If `attempts` were incremented at failure time instead of at claim
      // time, this crash -- which reports nothing at all -- would never
      // increment it, and the loop below would spin forever.
      expect(claimed!.attempts).toBe(attempt);
      await expireLease('v0');
      await reapOrphans(db, OPTS);
    }

    expect(await claimOne(db, OPTS)).toBeNull();
    const row = await readVideo('v0');
    expect(row.status).toBe('failed');
    expect(row.attempts).toBe(MAX_ATTEMPTS);
    expect(row.lastError).toMatch(/attempt/i);
  });
});

/**
 * Phase 3 (group B) -- `/retry` support. `resetFailedForProfile` is the
 * companion to `jobs.repository.ts`'s `retry`: a job moving `blocked|failed`
 * -> `pending` is only half the story, because failed VIDEOS are what
 * actually need to run again.
 */
describe('resetFailedForProfile', () => {
  it('resets every failed video for the profile to pending, with attempts zeroed', async () => {
    await seedPending(2);
    await pool.query(
      "UPDATE videos SET status = 'failed', attempts = $1, last_error = 'boom', lease_expires_at = now() WHERE aweme_id = 'v0'",
      [MAX_ATTEMPTS],
    );

    const resetCount = await resetFailedForProfile(db, 'profile-1');

    expect(resetCount).toBe(1);
    const row = await readVideo('v0');
    expect(row.status).toBe('pending');
    expect(row.attempts).toBe(0);
    expect(row.lastError).toBeNull();
    expect(row.leaseExpiresAt).toBeNull();
    // v1 was never failed -- must be left exactly alone.
    expect((await readVideo('v1')).status).toBe('pending');
  });

  it('does not touch videos that are not failed, and returns 0', async () => {
    await seedPending(1);

    expect(await resetFailedForProfile(db, 'profile-1')).toBe(0);
    expect((await readVideo('v0')).status).toBe('pending');
  });

  it(
    'is the only thing that makes an exhausted video claimable again (F6): zeroing ' +
      'attempts is the whole point, not a cleanup detail -- claimOne filters on ' +
      'attempts < maxAttempts, so a status-only reset would produce a row that is ' +
      'pending forever and never claimable',
    async () => {
      await seedPending(1);
      await pool.query("UPDATE videos SET status = 'failed', attempts = $1 WHERE aweme_id = 'v0'", [
        MAX_ATTEMPTS,
      ]);

      await resetFailedForProfile(db, 'profile-1');
      const claimed = await claimOne(db, OPTS);

      expect(claimed?.awemeId).toBe('v0');
      expect(claimed?.attempts).toBe(1);
    },
  );
});

describe('listByProfile', () => {
  it('lists a profiles videos newest first, paginated, with a total unaffected by the page', async () => {
    await seedPending(3);

    const page = await listByProfile(db, 'profile-1', { limit: 2, offset: 0 });

    expect(page.total).toBe(3);
    expect(page.items.map((v) => v.awemeId)).toEqual(['v2', 'v1']);

    const next = await listByProfile(db, 'profile-1', { limit: 2, offset: 2 });
    expect(next.items.map((v) => v.awemeId)).toEqual(['v0']);
  });

  it('filters by status when one is given', async () => {
    await seedPending(2);
    await pool.query("UPDATE videos SET status = 'done' WHERE aweme_id = 'v0'");

    const page = await listByProfile(db, 'profile-1', { limit: 20, offset: 0, status: 'done' });

    expect(page.items.map((v) => v.awemeId)).toEqual(['v0']);
    expect(page.total).toBe(1);
  });

  it('never returns another profiles videos', async () => {
    await seedPending(1);
    await db
      .insert(videos)
      .values({
        awemeId: 'other-profile-video',
        secUserId: 'profile-2',
        collectionId: collectionOf['profile-2'],
        createTime: new Date(),
      });

    const page = await listByProfile(db, 'profile-1', { limit: 20, offset: 0 });

    expect(page.items.map((v) => v.awemeId)).toEqual(['v0']);
  });
});

describe('countByStatusForProfile', () => {
  it('groups counts by status, scoped to one profile (F7 -- progress is measured against profile state)', async () => {
    await seedPending(3);
    await pool.query("UPDATE videos SET status = 'done' WHERE aweme_id IN ('v0', 'v1')");
    await db
      .insert(videos)
      .values({
        awemeId: 'other-profile-done',
        secUserId: 'profile-2',
        collectionId: collectionOf['profile-2'],
        createTime: new Date(),
        status: 'done',
      });

    const counts = await countByStatusForProfile(db, 'profile-1');

    expect(counts).toEqual({ done: 2, pending: 1 });
  });

  it('returns an empty object for a profile with no videos', async () => {
    expect(await countByStatusForProfile(db, 'nobody')).toEqual({});
  });
});

/**
 * Phase 5 -- the enumerate lane's only write to this table.
 *
 * Two invariants live in one statement, and both are load-bearing:
 *
 *   ON CONFLICT DO NOTHING  is what makes an incremental re-run free (F7).
 *                           `aweme_id` is the global key, so a second job over
 *                           the same profile must re-insert nothing.
 *   play_addr + url_expires_at  are written HERE, from the enumerate response
 *                           (F1). Resolving them per video later costs ~525
 *                           requests for a 500-video profile against ~25,
 *                           which is the difference between working and being
 *                           rate-limited into a ban.
 */
describe('upsertMany (phase 5)', () => {
  const row = (awemeId: string, overrides: Record<string, unknown> = {}) => ({
    awemeId,
    secUserId: 'profile-1',
    desc: `clip ${awemeId}`,
    createTime: new Date(Date.UTC(2026, 0, 2)),
    durationMs: 15_000,
    playAddr: `https://cdn.example/${awemeId}.mp4`,
    collectionId: collectionOf[(overrides.secUserId as 'profile-2' | undefined) ?? 'profile-1'],
    ...overrides,
  });

  it('inserts new videos with play_addr and a url_expires_at in the future (F1)', async () => {
    const inserted = await upsertMany(db, [row('a1'), row('a2')], { urlTtlSeconds: 7200 });

    expect(inserted).toBe(2);
    const stored = await readVideo('a1');
    expect(stored.playAddr).toBe('https://cdn.example/a1.mp4');
    // Present IMMEDIATELY after enumerate -- not filled in later by the
    // download lane. That is the whole of F1's saving.
    expect(stored.urlExpiresAt).not.toBeNull();
    expect(stored.urlExpiresAt!.getTime()).toBeGreaterThan(Date.now());
    expect(stored.status).toBe('pending');
    expect(stored.desc).toBe('clip a1');
    expect(stored.durationMs).toBe(15_000);
  });

  it('computes url_expires_at from the DATABASE clock, not this process clock', async () => {
    // Same rule as every lease in this schema. Two lane hosts with a few
    // seconds of skew must not disagree about whether a cached URL is still
    // usable, or phase 6 re-resolves URLs that were fine and skips ones that
    // were not.
    const ttl = 3600;
    const before = Date.now();
    await upsertMany(db, [row('a-clock')], { urlTtlSeconds: ttl });
    const [{ dbnow }] = (await pool.query('SELECT now() AS dbnow')).rows as { dbnow: Date }[];

    const stored = await readVideo('a-clock');
    const drift = Math.abs(stored.urlExpiresAt!.getTime() - (dbnow.getTime() + ttl * 1000));
    expect(drift).toBeLessThan(2_000);
    // And it is genuinely a TTL ahead, not just "some timestamp".
    expect(stored.urlExpiresAt!.getTime()).toBeGreaterThan(before + ttl * 1000 - 5_000);
  });

  it('inserts nothing on a re-run of the same page (ON CONFLICT DO NOTHING)', async () => {
    await upsertMany(db, [row('a1'), row('a2')], { urlTtlSeconds: 7200 });

    const inserted = await upsertMany(db, [row('a1'), row('a2')], { urlTtlSeconds: 7200 });

    expect(inserted).toBe(0);
    expect(await countByStatusForProfile(db, 'profile-1')).toEqual({ pending: 2 });
  });

  it('counts only the rows it actually inserted when a page half-overlaps', async () => {
    // The normal incremental case: a profile gained two videos since last time,
    // and the first page carries them alongside twenty it already has. The
    // return value is what the lane reports, so it has to be the new ones.
    await upsertMany(db, [row('old-1'), row('old-2')], { urlTtlSeconds: 7200 });

    expect(
      await upsertMany(db, [row('old-1'), row('new-1'), row('old-2')], { urlTtlSeconds: 7200 }),
    ).toBe(1);
  });

  it('leaves a video that is already done completely untouched', async () => {
    // The reason DO NOTHING beats DO UPDATE here. A re-run re-enumerates rows
    // whose files are already on disk; overwriting their status, or resetting
    // `attempts`, would re-download the entire archive on every job -- which is
    // exactly the dedupe F7 exists to provide.
    await upsertMany(db, [row('a-done')], { urlTtlSeconds: 7200 });
    await markDone(db, 'a-done', '/data/douyin/profile-1/a-done.mp4');

    await upsertMany(db, [row('a-done', { playAddr: 'https://cdn.example/CHANGED.mp4' })], {
      urlTtlSeconds: 7200,
    });

    const stored = await readVideo('a-done');
    expect(stored.status).toBe('done');
    expect(stored.storagePath).toBe('/data/douyin/profile-1/a-done.mp4');
    expect(stored.playAddr).toBe('https://cdn.example/a-done.mp4');
  });

  it('is a no-op on an empty page rather than invalid SQL', async () => {
    // Drizzle emits `insert into ... values ()` for an empty array, which
    // Postgres rejects. The lane reaches this on the last page of a profile
    // whose item count is an exact multiple of the page size, so it is a
    // routine path, not a defensive one.
    await expect(upsertMany(db, [], { urlTtlSeconds: 7200 })).resolves.toBe(0);
  });

  it('carries an explicit `skipped` status and its reason into the row', async () => {
    // 图文 image posts (aweme.mapper.ts). The status has to be written by the
    // INSERT, not by a follow-up UPDATE: `ON CONFLICT DO NOTHING` means there is
    // exactly one moment in a row's life when the enumerate lane can say
    // anything about it at all.
    await upsertMany(
      db,
      [row('a-image', { status: 'skipped' as const, lastError: 'no video in this post' })],
      { urlTtlSeconds: 7200 },
    );

    const stored = await readVideo('a-image');
    expect(stored.status).toBe('skipped');
    expect(stored.lastError).toBe('no video in this post');
  });

  it('writes a MIXED page -- one pending, one skipped -- in the same statement', async () => {
    // The shape a real page has, and the reason `upsertMany` writes `status`
    // explicitly instead of leaving it to the column default: drizzle builds ONE
    // column list for a multi-row insert, so a page holding a row with no
    // `status` next to a row with `'skipped'` would be relying on how drizzle
    // fills the gap. Two rows, one statement, is the case that pins it.
    const inserted = await upsertMany(
      db,
      [row('a-video'), row('a-image', { status: 'skipped' as const, lastError: 'no video' })],
      { urlTtlSeconds: 7200 },
    );

    expect(inserted).toBe(2);
    expect(await countByStatusForProfile(db, 'profile-1')).toEqual({ pending: 1, skipped: 1 });
    // Not a downgrade of the other row: the pending one keeps everything a
    // downloadable video needs.
    const video = await readVideo('a-video');
    expect(video.lastError).toBeNull();
    expect(video.playAddr).toBe('https://cdn.example/a-video.mp4');
    expect(video.urlExpiresAt).not.toBeNull();
  });

  it('leaves a `skipped` row where the claim query cannot see it', async () => {
    // The only statement that means "nothing will be downloaded for this row".
    await upsertMany(db, [row('a-image', { status: 'skipped' as const })], {
      urlTtlSeconds: 7200,
    });

    await expect(claimOne(db, OPTS)).resolves.toBeNull();
  });

  it('survives a page that repeats an item, inserting it once', async () => {
    // Douyin has been seen to repeat an item across a cursor boundary. `DO
    // NOTHING` handles that inside one statement -- measured, not assumed. It is
    // also the second, independent reason `DO UPDATE` is not an option: the same
    // input makes `DO UPDATE` raise "cannot affect row a second time", turning a
    // repeated item from a non-event into a failed tick.
    expect(await upsertMany(db, [row('dup'), row('dup')], { urlTtlSeconds: 7200 })).toBe(1);
    expect(await countByStatusForProfile(db, 'profile-1')).toEqual({ pending: 1 });
  });

  it('writes the collection, episode and manual flag each row carries', async () => {
    await upsertMany(
      db,
      [
        row('c1'),
        row('c2', { secUserId: 'profile-2', mixEpisode: 4, collectionManual: true }),
      ],
      { urlTtlSeconds: 7200 },
    );

    expect(await readVideo('c1')).toMatchObject({
      collectionId: collectionOf['profile-1'],
      mixEpisode: null,
      collectionManual: false,
    });
    expect(await readVideo('c2')).toMatchObject({
      collectionId: collectionOf['profile-2'],
      mixEpisode: 4,
      collectionManual: true,
    });
  });

  it('creates no collection itself: assignment is decided before the insert', async () => {
    // A channel with a profile but no channel collection yet.
    await pool.query(`INSERT INTO profiles (sec_user_id) VALUES ('bare')`);
    const before = (await pool.query('SELECT count(*)::int AS n FROM collections')).rows[0].n;

    await upsertMany(db, [row('n1', { secUserId: 'bare', collectionId: collectionOf['profile-1'] })], {
      urlTtlSeconds: 7200,
    });

    expect((await readVideo('n1')).collectionId).toBe(collectionOf['profile-1']);

    expect((await pool.query('SELECT count(*)::int AS n FROM collections')).rows[0].n).toBe(before);
  });

  it('refuses a row without a collection instead of storing a video that belongs nowhere', async () => {
    // The profile-less channel is now refused by `resolveCollections`, before
    // any row is built; this is the last line: `collection_id` is NOT NULL.
    await expect(
      upsertMany(db, [row('ghost', { collectionId: undefined as unknown as string })], {
        urlTtlSeconds: 7200,
      }),
    ).rejects.toMatchObject({ cause: { code: '23502', column: 'collection_id' } });
    await expect(readVideo('ghost')).rejects.toThrow(/not found/);
  });
});
