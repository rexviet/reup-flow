/**
 * Phase 1 -- `jobs` claim/checkpoint/reaper repository (phase-01 §1.3, F5).
 *
 * The job-side mirror of videos.repository.spec.ts. F5 exists because the
 * failure it prevents is silent: a crash mid-enumerate leaves the job in
 * `enumerating`, the lane only ever picks up `pending`, and the job is dead
 * with no error, no progress, and nothing in the logs to say so.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import {
  cancel,
  checkpoint,
  claimOne,
  create,
  findById,
  finishEnumerate,
  list,
  markBlocked,
  reapOrphans,
  releaseClaim,
  retry,
  upsertProfile,
} from '../../src/db/jobs.repository';
import { jobs, profiles, videos, type Job } from '../../src/db/schema';
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

const SCHEMA = testSchemaName('jobs');
const LEASE_SECONDS = 300;
const OPTS = { leaseSeconds: LEASE_SECONDS };
const CONCURRENCY_STATEMENT_TIMEOUT_MS = 500;

let pool: Pool;
let db: ReturnType<typeof testDb>;

/** Seeds `count` pending jobs, oldest first, returning their ids in order. */
async function seedPendingJobs(count: number): Promise<string[]> {
  await db.insert(profiles).values({ secUserId: 'profile-1' }).onConflictDoNothing();
  const rows = await db
    .insert(jobs)
    .values(
      Array.from({ length: count }, (_, i) => ({
        secUserId: 'profile-1',
        createdAt: new Date(Date.UTC(2026, 0, 1) + i * 60_000),
      })),
    )
    .returning();
  return rows.sort((a, b) => a.createdAt.getTime() - b.createdAt.getTime()).map((r) => r.id);
}

async function readJob(id: string): Promise<Job> {
  const [row] = await db.select().from(jobs).where(eq(jobs.id, id));
  if (!row) throw new Error(`job ${id} not found`);
  return row;
}

/** Simulates the worker dying and the lease elapsing, without sleeping. */
async function expireLease(id: string): Promise<void> {
  await pool.query(
    "UPDATE jobs SET lease_expires_at = now() - interval '1 hour' WHERE id = $1",
    [id],
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
  it('claims the oldest pending job and puts it under lease', async () => {
    const [oldest] = await seedPendingJobs(3);

    const claimed = await claimOne(db, OPTS);

    expect(claimed!.id).toBe(oldest);
    expect(claimed!.status).toBe('enumerating');
    const deltaMs = claimed!.leaseExpiresAt!.getTime() - Date.now();
    expect(deltaMs).toBeGreaterThan((LEASE_SECONDS - 60) * 1000);
    expect(deltaMs).toBeLessThan((LEASE_SECONDS + 60) * 1000);
  });

  it('returns null when there is nothing pending', async () => {
    expect(await claimOne(db, OPTS)).toBeNull();
  });

  it('does not re-claim a job that is already being enumerated', async () => {
    await seedPendingJobs(1);
    expect(await claimOne(db, OPTS)).not.toBeNull();
    expect(await claimOne(db, OPTS)).toBeNull();
  });

  it('concurrent claimers never hand the same job to two lanes', async () => {
    const N = 3;
    const ids = await seedPendingJobs(N);

    const pools = Array.from({ length: N + 2 }, () =>
      createTestPool(SCHEMA, { statementTimeoutMs: CONCURRENCY_STATEMENT_TIMEOUT_MS, max: 1 }),
    );
    try {
      await Promise.all(pools.map((p) => p.query('SELECT 1')));
      const dbs = pools.map((p) => testDb(p));

      const { outcomes, intervals } = await runConcurrentlyHoldingLocks(dbs, (tx) =>
        claimOne(tx, OPTS),
      );
      assertGenuineOverlap(intervals);

      expect(outcomes.filter((o) => o.status === 'rejected')).toHaveLength(0);
      const claimedIds = outcomes
        .map((o) => (o.status === 'fulfilled' ? o.value : null))
        .filter((j): j is Job => j !== null)
        .map((j) => j.id);

      expect(claimedIds).toHaveLength(N);
      expect(new Set(claimedIds)).toEqual(new Set(ids));
    } finally {
      await Promise.all(pools.map((p) => p.end()));
    }
  });
});

describe('checkpoint', () => {
  it('records the cursor and count, and renews the lease', async () => {
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await expireLease(id!);

    const stillOurs = await checkpoint(db, id!, {
      maxCursor: 1735689600000,
      enumeratedCount: 20,
      leaseSeconds: LEASE_SECONDS,
    });

    expect(stillOurs).toBe(true);
    const row = await readJob(id!);
    // bigint round-trips as a JS number, not a string -- max_cursor is a
    // millisecond timestamp, comfortably inside MAX_SAFE_INTEGER.
    expect(row.maxCursor).toBe(1735689600000);
    expect(row.enumeratedCount).toBe(20);
    // The renewal is the point: a long enumerate must be able to keep its
    // lease alive without finishing, or the reaper takes the job away from
    // a lane that is still working.
    expect(row.leaseExpiresAt!.getTime()).toBeGreaterThan(Date.now());
  });

  it('reports that the job is gone once it has been reaped away', async () => {
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await expireLease(id!);
    await reapOrphans(db);

    const stillOurs = await checkpoint(db, id!, {
      maxCursor: 999,
      enumeratedCount: 1,
      leaseSeconds: LEASE_SECONDS,
    });

    expect(stillOurs).toBe(false);
    expect((await readJob(id!)).maxCursor).toBe(0);
  });

  it('never rewinds the cursor when a stale worker checkpoints late', async () => {
    // A reaped worker can still be mid-flight when the replacement lane has
    // already advanced past it. Its late checkpoint must not drag the
    // cursor backwards and make the replacement re-enumerate pages it
    // already has.
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await checkpoint(db, id!, { maxCursor: 500, enumeratedCount: 40, leaseSeconds: LEASE_SECONDS });

    await checkpoint(db, id!, { maxCursor: 200, enumeratedCount: 10, leaseSeconds: LEASE_SECONDS });

    const row = await readJob(id!);
    expect(row.maxCursor).toBe(500);
    expect(row.enumeratedCount).toBe(40);
  });

  it('accepts a DESCENDING cursor as long as the item count advanced (phase 5)', async () => {
    // Phase 5 found the flaw in the first version of the rule above. It read
    // "never rewinds" as `max_cursor = greatest(old, new)` -- monotonic in the
    // cursor's own NUMERIC value -- and that is an assumption about Douyin's
    // paging that Douyin does not honour.
    //
    // `aweme/post` returns `max_cursor` as the create_time (ms) of the OLDEST
    // item in the page, and a profile pages newest-first. So the cursor DROPS
    // on every page. Under `greatest` the checkpoint freezes at page one's
    // value for the rest of the job: `enumerated_count` keeps climbing, the
    // lease keeps renewing, everything looks healthy -- and a restart re-pages
    // the whole profile from page two, spending the F2 request budget the
    // checkpoint existed to protect.
    //
    // It would also have passed every test in this file, because a fake parser
    // is free to hand back ascending numbers. That is the exact false-green
    // shape this repo keeps finding.
    //
    // So the yardstick is PROGRESS, not the cursor's magnitude:
    // `enumerated_count` only ever grows, whichever way Douyin's cursor moves,
    // so a checkpoint carrying more items is by definition further along. The
    // straggler protection the test above asserts is unchanged -- a stale
    // worker has seen FEWER items, so it still cannot rewind anything.
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await checkpoint(db, id!, {
      maxCursor: 1735689600000,
      enumeratedCount: 20,
      leaseSeconds: LEASE_SECONDS,
    });

    await checkpoint(db, id!, {
      maxCursor: 1735603200000, // one day EARLIER -- the next page back
      enumeratedCount: 40,
      leaseSeconds: LEASE_SECONDS,
    });

    const row = await readJob(id!);
    expect(row.maxCursor).toBe(1735603200000);
    expect(row.enumeratedCount).toBe(40);
  });

  it('reports NOT-OURS when the write was discarded, so a straggler stops paging', async () => {
    // Phase 5 review. The rule above discards a stale checkpoint's cursor -- but
    // the first version still answered `true`, which tells the straggler "still
    // yours, keep going". Two concurrent request streams against an endpoint
    // that rate-limits by IP is a live F2 violation, and it lasts as long as the
    // straggler does.
    //
    // The new rule makes the fix nearly free: "did this write land" is exactly
    // the predicate the SET already computes, so it moves into the WHERE and the
    // lane's existing `if (!stillOurs) return 'lost'` path stops the straggler
    // dead. This is the one place the honest "arithmetic is not a guard" note
    // above `finishEnumerate` turns into an actual guard.
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await checkpoint(db, id!, { maxCursor: 500, enumeratedCount: 40, leaseSeconds: LEASE_SECONDS });

    const stillOurs = await checkpoint(db, id!, {
      maxCursor: 200,
      enumeratedCount: 10,
      leaseSeconds: LEASE_SECONDS,
    });

    expect(stillOurs).toBe(false);
    // And it really did leave the row alone -- "false" must not be the sound of
    // a write that happened anyway.
    const row = await readJob(id!);
    expect(row.maxCursor).toBe(500);
    expect(row.enumeratedCount).toBe(40);
  });

  it('does NOT renew the lease for a checkpoint it discarded', async () => {
    // Following from the above: a straggler being told to stop must not also be
    // renewing the lease on its way out. Renewing would hold the job away from
    // the replacement for another LEASE_SECONDS on the strength of work that
    // was rejected.
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await checkpoint(db, id!, { maxCursor: 500, enumeratedCount: 40, leaseSeconds: LEASE_SECONDS });
    await pool.query(
      "UPDATE jobs SET lease_expires_at = now() + interval '2 seconds' WHERE id = $1",
      [id],
    );

    await checkpoint(db, id!, { maxCursor: 200, enumeratedCount: 10, leaseSeconds: LEASE_SECONDS });

    expect((await readJob(id!)).leaseExpiresAt!.getTime()).toBeLessThan(Date.now() + 10_000);
  });

  it('ignores a stale checkpoint that carries the SAME item count', async () => {
    // The boundary of the rule. Two lanes that have each seen 20 items have no
    // ordering between them, so the second one to arrive must not overwrite the
    // first -- strictly greater, not greater-or-equal. Without this a straggler
    // whose count happens to match the replacement's still rewinds the cursor,
    // which is the whole failure the rule above exists to prevent.
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await checkpoint(db, id!, { maxCursor: 500, enumeratedCount: 20, leaseSeconds: LEASE_SECONDS });

    await checkpoint(db, id!, { maxCursor: 900, enumeratedCount: 20, leaseSeconds: LEASE_SECONDS });

    expect((await readJob(id!)).maxCursor).toBe(500);
  });
});

/**
 * Phase 5 -- the three ways an enumerate claim ends.
 *
 * All three are conditional UPDATEs gated on `status = 'enumerating'`, for the
 * same reason `cancel`/`retry` are: a lane that was reaped mid-flight must not
 * be able to write over a row that now belongs to somebody else. The status
 * gate is what makes each of these a no-op once the claim is gone, instead of a
 * silent stomp on a replacement's work.
 */

/** Seeds one video for `profile-1`, so total_videos has something to count. */
async function seedVideos(count: number): Promise<void> {
  await db.insert(profiles).values({ secUserId: 'profile-1' }).onConflictDoNothing();
  if (count === 0) return;
  await db.insert(videos).values(
    Array.from({ length: count }, (_, i) => ({
      awemeId: `aweme-${i}`,
      secUserId: 'profile-1',
      createTime: new Date(Date.UTC(2026, 0, 1) + i * 1000),
    })),
  );
}

describe('finishEnumerate', () => {
  it('moves the job to downloading and counts total_videos off the profile', async () => {
    const [id] = await seedPendingJobs(1);
    await seedVideos(30);
    await claimOne(db, OPTS);

    expect(await finishEnumerate(db, id!)).toBe(true);

    const row = await readJob(id!);
    expect(row.status).toBe('downloading');
    // Counted in SQL from PROFILE state, not handed in by the lane. A lane that
    // added up page sizes would double-count anything a previous job already
    // had (F7 -- `videos` is keyed globally on `aweme_id`), and "completed,
    // total 60, 30 files on disk" is a bug report waiting to happen.
    expect(row.totalVideos).toBe(30);
    // The lease is handed back here, not left to expire: the enumerate lane is
    // done with this job and the reaper must not spend a sweep on it.
    expect(row.leaseExpiresAt).toBeNull();
  });

  it('does nothing to a job that is no longer being enumerated', async () => {
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await expireLease(id!);
    await reapOrphans(db);

    expect(await finishEnumerate(db, id!)).toBe(false);
    expect((await readJob(id!)).status).toBe('pending');
  });
});

describe('markBlocked', () => {
  it('sets blocked, keeps max_cursor, and records the reason', async () => {
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await checkpoint(db, id!, { maxCursor: 4242, enumeratedCount: 20, leaseSeconds: LEASE_SECONDS });

    expect(await markBlocked(db, id!, 'cookie appears to be blocked')).toBe(true);

    const row = await readJob(id!);
    expect(row.status).toBe('blocked');
    // THE point of `blocked` over `failed`: the checkpoint survives, so pushing
    // a fresh cookie and hitting `/retry` resumes from here instead of spending
    // the whole F2 request budget re-paging the profile from the top.
    expect(row.maxCursor).toBe(4242);
    expect(row.error).toBe('cookie appears to be blocked');
    expect(row.leaseExpiresAt).toBeNull();
  });

  it('redacts the reason on the way IN, not just on the way out', async () => {
    // Same rule as `videos.repository.ts`'s `markFailed`, and for the same
    // reason (plan §0 principle 3): phase 7 will call this with text that came
    // from Douyin or from a parser error, so scrubbing only in the mapper would
    // still leave the raw secret sitting in a column any `psql` session -- or
    // any backup -- reads directly.
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);

    await markBlocked(db, id!, 'refused with sessionid=abcdef0123456789abcdef');

    expect((await readJob(id!)).error).not.toContain('abcdef0123456789abcdef');
  });

  it('does nothing to a job that is no longer being enumerated', async () => {
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await expireLease(id!);
    await reapOrphans(db);

    expect(await markBlocked(db, id!, 'too late')).toBe(false);
    const row = await readJob(id!);
    expect(row.status).toBe('pending');
    expect(row.error).toBeNull();
  });
});

describe('releaseClaim', () => {
  it('hands the job back to pending with its checkpoint intact', async () => {
    // §4.2's "nhả lease" for the enumerate lane: on SIGTERM the job goes back
    // on the queue immediately, so the restarted process can claim it at once
    // instead of idling beside it for a full LEASE_SECONDS.
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await checkpoint(db, id!, { maxCursor: 77, enumeratedCount: 20, leaseSeconds: LEASE_SECONDS });

    expect(await releaseClaim(db, id!)).toBe(true);

    const row = await readJob(id!);
    expect(row.status).toBe('pending');
    expect(row.leaseExpiresAt).toBeNull();
    expect(row.maxCursor).toBe(77);
  });

  it('is immediately re-claimable after release -- the whole point of releasing', async () => {
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await releaseClaim(db, id!);

    expect((await claimOne(db, OPTS))!.id).toBe(id);
  });

  it('does NOT hand back a job that has already moved on', async () => {
    // The window `lane.runner.ts` warns about: `release()` runs after the
    // drain, and the drain can time out, so in the worst case this fires while
    // a tick is still in flight. An unconditional `SET status = 'pending'`
    // could then put a row back on the queue that has since been finished --
    // and a `downloading` job dragged back to `pending` gets its whole profile
    // re-enumerated.
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await finishEnumerate(db, id!);

    expect(await releaseClaim(db, id!)).toBe(false);
    expect((await readJob(id!)).status).toBe('downloading');
  });
});

describe('reapOrphans (F5)', () => {
  it('hands back a job whose lane died mid-enumerate', async () => {
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await expireLease(id!);

    const reaped = await reapOrphans(db);

    expect(reaped.map((j) => j.id)).toEqual([id]);
    const row = await readJob(id!);
    expect(row.status).toBe('pending');
    expect(row.leaseExpiresAt).toBeNull();
  });

  it('does not take a job away from a lane whose lease is still live', async () => {
    await seedPendingJobs(1);
    await claimOne(db, OPTS);

    expect(await reapOrphans(db)).toEqual([]);
  });

  it('resumes from the checkpoint rather than from the beginning', async () => {
    // The end-to-end shape of F5: enumerate half a profile, die, get reaped,
    // get re-claimed -- and pick up at the cursor, not at page one.
    const [id] = await seedPendingJobs(1);
    await claimOne(db, OPTS);
    await checkpoint(db, id!, { maxCursor: 1234, enumeratedCount: 40, leaseSeconds: LEASE_SECONDS });
    await expireLease(id!);

    await reapOrphans(db);
    const reclaimed = await claimOne(db, OPTS);

    expect(reclaimed!.id).toBe(id);
    expect(reclaimed!.maxCursor).toBe(1234);
    expect(reclaimed!.enumeratedCount).toBe(40);
  });
});

/**
 * Phase 3 (group B) -- the HTTP-facing job operations. Unlike claim/checkpoint
 * /reapOrphans above, none of these are on the lane's hot path; they exist so
 * `jobs.controller.ts` never has to hand-roll SQL or hold the `HTTP layer
 * writes a row and stops` invariant (plan §0 principle 1) by convention alone.
 */
describe('upsertProfile', () => {
  it('inserts a new profile row', async () => {
    await upsertProfile(db, 'profile-new');

    const [row] = await db.select().from(profiles).where(eq(profiles.secUserId, 'profile-new'));
    expect(row).toBeDefined();
  });

  it('is a no-op on a profile that already exists -- jobs.sec_user_id has an FK to profiles, so a plain INSERT on a repeat job for the same profile would fail with a foreign-key-adjacent unique violation instead of silently succeeding', async () => {
    await upsertProfile(db, 'profile-new');

    await expect(upsertProfile(db, 'profile-new')).resolves.toBeUndefined();
    const rows = await db.select().from(profiles).where(eq(profiles.secUserId, 'profile-new'));
    expect(rows).toHaveLength(1);
  });
});

describe('create', () => {
  it('inserts a pending job for the given profile', async () => {
    await upsertProfile(db, 'profile-new');

    const job = await create(db, 'profile-new');

    expect(job.status).toBe('pending');
    expect(job.secUserId).toBe('profile-new');
    expect(await findById(db, job.id)).toEqual(job);
  });
});

describe('list', () => {
  it('returns jobs newest first, paginated, with a total unaffected by the page', async () => {
    await upsertProfile(db, 'profile-new');
    const ids: string[] = [];
    for (let i = 0; i < 3; i++) {
      const job = await create(db, 'profile-new');
      // Force distinct, known createdAt ordering -- inserts issued back to
      // back can otherwise land in the same millisecond, which would make
      // "newest first" ambiguous and the assertion below flaky.
      await pool.query('UPDATE jobs SET created_at = $1 WHERE id = $2', [
        new Date(Date.UTC(2026, 0, 1) + i * 60_000),
        job.id,
      ]);
      ids.push(job.id);
    }

    const page = await list(db, { limit: 2, offset: 0 });

    expect(page.total).toBe(3);
    expect(page.items.map((j) => j.id)).toEqual([ids[2], ids[1]]);

    const nextPage = await list(db, { limit: 2, offset: 2 });
    expect(nextPage.items.map((j) => j.id)).toEqual([ids[0]]);
  });
});

describe('findById', () => {
  it('returns null for an id that does not exist', async () => {
    expect(await findById(db, '00000000-0000-0000-0000-000000000000')).toBeNull();
  });
});

describe('cancel', () => {
  it('cancels a pending job', async () => {
    await upsertProfile(db, 'profile-new');
    const job = await create(db, 'profile-new');

    const cancelled = await cancel(db, job.id);

    expect(cancelled?.status).toBe('cancelled');
  });

  it.each(['enumerating', 'downloading', 'blocked'])('cancels a job in status %s', async (status) => {
    await upsertProfile(db, 'profile-new');
    const job = await create(db, 'profile-new');
    await pool.query('UPDATE jobs SET status = $1 WHERE id = $2', [status, job.id]);

    expect((await cancel(db, job.id))?.status).toBe('cancelled');
  });

  it.each(['completed', 'failed', 'cancelled'])(
    'refuses to cancel a job already in the terminal status %s',
    async (status) => {
      await upsertProfile(db, 'profile-new');
      const job = await create(db, 'profile-new');
      await pool.query('UPDATE jobs SET status = $1 WHERE id = $2', [status, job.id]);

      expect(await cancel(db, job.id)).toBeNull();
      // The row is untouched -- a WHERE clause matching no candidate status
      // must not apply any partial effect.
      expect((await findById(db, job.id))?.status).toBe(status);
    },
  );

  it('returns null for an id that does not exist at all -- the caller tells "no such job" apart from "wrong state" with findById', async () => {
    expect(await cancel(db, '00000000-0000-0000-0000-000000000000')).toBeNull();
  });
});

describe('retry', () => {
  it.each(['blocked', 'failed'])('resets a job in status %s back to pending', async (status) => {
    await upsertProfile(db, 'profile-new');
    const job = await create(db, 'profile-new');
    await pool.query(
      "UPDATE jobs SET status = $1, error = 'boom', lease_expires_at = now() + interval '1 hour' WHERE id = $2",
      [status, job.id],
    );

    const retried = await retry(db, job.id);

    expect(retried?.status).toBe('pending');
    expect(retried?.error).toBeNull();
    expect(retried?.leaseExpiresAt).toBeNull();
  });

  // `cancelled` was in this list until phase 7 removed it, and the removal is a
  // FIX rather than a loosening: cancel has the download lane mark the profile's
  // outstanding videos `skipped`, and `skipped` was a status nothing reset, so a
  // cancelled profile could never be downloaded again -- silently, because
  // `skipped` counts as settled and the next job therefore reported `completed`
  // with zero downloads. See test/db/cancel-retry-revival.spec.ts and
  // `reviveSkippedForProfile`. What stays un-retryable is `completed` (nothing to
  // retry) and the two in-flight statuses (somebody else's claim).
  it.each(['pending', 'enumerating', 'downloading', 'completed'])(
    'refuses to retry a job in status %s',
    async (status) => {
      await upsertProfile(db, 'profile-new');
      const job = await create(db, 'profile-new');
      await pool.query('UPDATE jobs SET status = $1 WHERE id = $2', [status, job.id]);

      expect(await retry(db, job.id)).toBeNull();
    },
  );
});
