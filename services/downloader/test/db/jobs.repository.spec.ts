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
import { checkpoint, claimOne, reapOrphans } from '../../src/db/jobs.repository';
import { jobs, profiles, type Job } from '../../src/db/schema';
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
