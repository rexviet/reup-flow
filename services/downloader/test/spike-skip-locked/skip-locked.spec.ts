/**
 * SPIKE 1 (GATE) -- plan.md §3.1 / phase-00 §0.4.
 *
 * Question this file answers with runnable evidence, not doc-reading:
 * can Drizzle express `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP
 * LOCKED) RETURNING` without dropping to raw SQL, and does that expression
 * actually behave correctly under real concurrent Postgres transactions?
 *
 * "Generates the right SQL text" is NOT the gate -- see the false-green
 * class documented in docs/journals/20260824-phase-01-false-greens.md.
 * The gate is runtime behaviour of two real, concurrent, separately
 * connected transactions against a real Postgres (docker-compose `postgres`
 * service, host port 5432).
 */
import type { Pool } from 'pg';
import { TransactionRollbackError } from 'drizzle-orm';
import { claimOne } from './claim';
import { assertGenuineOverlap, runConcurrentlyHoldingLocks } from '../support/concurrency-harness';
import { spikeItems } from './schema';
import {
  STATEMENT_TIMEOUT_MS,
  createPool,
  createSpikeTable,
  db as makeDb,
  dropSpikeTable,
} from './db';

jest.setTimeout(30_000);

type ClaimRow = { id: number; status: string };

/**
 * Deliberately tight: tests 3 & 4 hold successful claims open until every
 * contender has settled (see concurrency-harness.ts), so a claimer that's
 * genuinely blocked on a missing SKIP LOCKED has nothing to wake up for and
 * must hit this timeout. Small enough to keep the suite fast, comfortably
 * larger than the sub-20ms it takes every claim in this suite to complete
 * when nothing is blocked.
 */
const CONCURRENCY_STATEMENT_TIMEOUT_MS = 500;

describe('spike: Drizzle FOR UPDATE SKIP LOCKED (gate)', () => {
  let setupPool: Pool;
  let seedDb: ReturnType<typeof makeDb>;

  beforeAll(async () => {
    setupPool = createPool();
    await createSpikeTable(setupPool);
    seedDb = makeDb(setupPool);
  });

  afterAll(async () => {
    await dropSpikeTable(setupPool);
    await setupPool.end();
  });

  afterEach(async () => {
    // Self-contained + re-runnable: every test starts from an empty table.
    await setupPool.query('TRUNCATE spike_items RESTART IDENTITY');
  });

  it('claim query text contains "for update skip locked" and needs no raw sql', async () => {
    const pool = createPool();
    try {
      const built = claimOne(makeDb(pool));
      const { sql } = built.toSQL();
      // eslint-disable-next-line no-console
      console.log('[spike-skip-locked] generated SQL:', sql);
      const normalized = sql.toLowerCase();
      expect(normalized).toContain('for update skip locked');
      expect(normalized).toContain('returning');
      expect(normalized).toContain('in (select');
    } finally {
      await pool.end();
    }
  });

  it(
    'B does not block on A\'s held lock: returns empty promptly, ' +
      'and the row is claimable again once A releases',
    async () => {
      await seedDb.insert(spikeItems).values({ status: 'pending' });

      const poolA = createPool({ statementTimeoutMs: STATEMENT_TIMEOUT_MS });
      const poolB = createPool({ statementTimeoutMs: STATEMENT_TIMEOUT_MS });
      try {
        const dbA = makeDb(poolA);
        const dbB = makeDb(poolB);

        let resultA: ClaimRow[] = [];
        let releaseA!: () => void;
        let signalAClaimed!: () => void;
        const holdA = new Promise<void>((resolve) => {
          releaseA = resolve;
        });
        const aClaimed = new Promise<void>((resolve) => {
          signalAClaimed = resolve;
        });

        const txPromiseA = dbA.transaction(async (tx) => {
          resultA = (await claimOne(tx)) as ClaimRow[];
          signalAClaimed();
          // Hold the transaction (and its row lock) open until the test says so.
          await holdA;
          // Roll back (rather than commit) so the row reverts to 'pending' --
          // this is what lets us prove afterwards that B never touched it:
          // if B had somehow claimed/deleted it, it would NOT be claimable
          // again here.
          tx.rollback();
        });

        await aClaimed;
        expect(resultA).toHaveLength(1);

        const startedAt = process.hrtime.bigint();
        let resultB: ClaimRow[] = [];
        let errorB: unknown = null;
        try {
          resultB = (await claimOne(dbB)) as ClaimRow[];
        } catch (err) {
          errorB = err;
        }
        const elapsedMs = Number(process.hrtime.bigint() - startedAt) / 1_000_000;

        releaseA();
        await expect(txPromiseA).rejects.toBeInstanceOf(TransactionRollbackError);

        // The whole point of the spike: B must NOT block, NOT time out, and
        // NOT get A's row.
        expect(errorB).toBeNull();
        expect(resultB).toHaveLength(0);
        expect(elapsedMs).toBeLessThan(1_000);

        // Prove B didn't consume/delete the row -- it's still claimable now
        // that A has released it.
        const resultAfter = (await claimOne(seedDb)) as ClaimRow[];
        expect(resultAfter).toHaveLength(1);
        expect(resultAfter[0]!.id).toBe(resultA[0]!.id);
      } finally {
        // try/finally so a failed assertion above can never leak either pool.
        await poolA.end();
        await poolB.end();
      }
    },
  );

  it('two concurrent claimers on separate connections never get the same id', async () => {
    await seedDb.insert(spikeItems).values([{ status: 'pending' }, { status: 'pending' }]);

    const pools = Array.from({ length: 2 }, () =>
      createPool({ statementTimeoutMs: CONCURRENCY_STATEMENT_TIMEOUT_MS, max: 1 }),
    );
    try {
      // Pre-warm both sockets so the barrier below is decided by the claim
      // query itself, not by connection setup latency.
      await Promise.all(pools.map((pool) => pool.query('SELECT 1')));
      const dbs = pools.map((pool) => makeDb(pool));

      // Real concurrency, not Promise.all-over-whole-transactions: every
      // claimer opens its transaction and parks before any of them runs
      // claimOne, and holds its claim open until every claimer has settled.
      // See ../support/concurrency-harness.ts for why both the barrier AND the hold
      // are needed -- proven: without the hold, this still passed with
      // SKIP LOCKED removed, because a released lock lets Postgres's
      // LockRows retry the next row and quietly self-heal.
      const { outcomes, intervals } = await runConcurrentlyHoldingLocks(dbs, (tx) => claimOne(tx));

      assertGenuineOverlap(intervals);

      const errors = outcomes.filter((o) => o.status === 'rejected').map((o) => o.error);
      expect(errors).toHaveLength(0);

      const [resultA, resultB] = outcomes.map((o) =>
        o.status === 'fulfilled' ? (o.value as ClaimRow[]) : [],
      );
      expect(resultA).toHaveLength(1);
      expect(resultB).toHaveLength(1);
      expect(resultA![0]!.id).not.toBe(resultB![0]!.id);
    } finally {
      await Promise.all(pools.map((pool) => pool.end()));
    }
  });

  it('N pending rows, N+2 concurrent claimers: exactly N distinct ids claimed, 2 get nothing', async () => {
    const N = 8;
    await seedDb
      .insert(spikeItems)
      .values(Array.from({ length: N }, () => ({ status: 'pending' })));

    const claimerCount = N + 2;
    const pools = Array.from({ length: claimerCount }, () =>
      createPool({ statementTimeoutMs: CONCURRENCY_STATEMENT_TIMEOUT_MS, max: 1 }),
    );
    try {
      // Pre-warm every connection before the barrier releases the claim itself.
      await Promise.all(pools.map((pool) => pool.query('SELECT 1')));
      const dbs = pools.map((pool) => makeDb(pool));

      const { outcomes, intervals } = await runConcurrentlyHoldingLocks(dbs, (tx) => claimOne(tx));

      assertGenuineOverlap(intervals);

      const errors = outcomes.filter((o) => o.status === 'rejected').map((o) => o.error);
      expect(errors).toHaveLength(0);

      const rows = outcomes.map((o) => (o.status === 'fulfilled' ? (o.value as ClaimRow[]) : []));
      const claimedIds = rows.filter((r) => r.length === 1).map((r) => r[0]!.id);
      const emptyCount = rows.filter((r) => r.length === 0).length;

      // Real overlap + SKIP LOCKED still yields exactly N distinct claims,
      // 2 empty, 0 errors: contenders racing an already-locked row simply
      // skip it and move to the next unlocked one -- that's the entire
      // point of SKIP LOCKED, so this outcome is not a coincidence of the
      // old (accidentally-serial) version of this test. Only WITHOUT
      // SKIP LOCKED does holding the winning claims open turn "real
      // overlap" into a structural failure: contenders blocked on an
      // already-(held-open)-locked row have no release to wake up for and
      // hit CONCURRENCY_STATEMENT_TIMEOUT_MS instead -- see the
      // mutation-proof note atop this suite and ../support/concurrency-harness.ts.
      expect(claimedIds).toHaveLength(N);
      expect(new Set(claimedIds).size).toBe(N);
      expect(emptyCount).toBe(2);
    } finally {
      await Promise.all(pools.map((pool) => pool.end()));
    }
  });
});
