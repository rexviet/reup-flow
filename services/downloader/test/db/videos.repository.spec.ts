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
  markDone,
  markFailed,
  reapOrphans,
  release,
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

/** Seeds `count` pending videos, oldest first, ids v0..v{count-1}. */
async function seedPending(count: number): Promise<void> {
  await db.insert(videos).values(
    Array.from({ length: count }, (_, i) => ({
      awemeId: `v${i}`,
      secUserId: 'profile-1',
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
