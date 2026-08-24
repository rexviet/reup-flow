/**
 * Barrier used by the SKIP LOCKED spike (phase 0) and the `videos` claim
 * suite (phase 1) to force genuine overlap between N
 * concurrent claimers, AND to force any genuinely-blocked contender to stay
 * blocked long enough to prove it -- rather than being let off the hook by
 * an early commit.
 *
 * Why this exists (part 1 -- overlap): `Promise.all([dbA.transaction(cb),
 * dbB.transaction(cb)])` does NOT guarantee the transactions' bodies run
 * concurrently. Drizzle's `.transaction()` is a client-side
 * BEGIN/.../COMMIT round-trip chain, and against a local Postgres one
 * claimer's whole transaction can finish before the next claimer even
 * issues BEGIN -- "concurrent" in source, serial on the wire. That is the
 * confirmed FG-1 false green: tests named for concurrency passed with the
 * row lock removed entirely because they never actually overlapped.
 *
 * Why this exists (part 2 -- holding locks open): overlap alone is not
 * enough to distinguish `FOR UPDATE SKIP LOCKED` from plain `FOR UPDATE`.
 * Empirically (verified while building this fix), Postgres's `LockRows`
 * plan node retries the next candidate row after a blocked lock is released
 * and its recheck (EvalPlanQual) fails -- so if every claimer commits
 * (and releases its lock) within microseconds, `FOR UPDATE` without
 * `SKIP LOCKED` still converges to the *same* correct claim set, just via
 * blocking instead of skipping. That makes the naive "release a barrier,
 * then Promise.all the claims" version of this harness unable to tell the
 * two apart. To make the difference observable, every successful claimer
 * holds its transaction (and lock) open until *every* claimer has either
 * succeeded-and-is-holding or already failed. A genuinely blocked
 * contender (no SKIP LOCKED) then has nothing to wake up for and is forced
 * to sit on the lock until its connection's `statement_timeout` cancels
 * the query -- a loud, structural failure instead of a quiet, accidental
 * pass.
 */

export interface ClaimInterval {
  start: bigint;
  end: bigint;
}

export type ConcurrentClaimOutcome<T> =
  | { status: 'fulfilled'; value: T }
  | { status: 'rejected'; error: unknown };

export interface ConcurrentRunResult<T> {
  outcomes: ConcurrentClaimOutcome<T>[];
  intervals: ClaimInterval[];
}

/**
 * Structural view of whatever the caller's transaction-capable handle is
 * (a Drizzle `NodePgDatabase`, in both callers today). Kept generic in
 * `TTx` so this harness stays independent of any one schema's executor
 * type -- phase 0's spike and phase 1's `videos` claim both drive it.
 */
export interface TransactionalDb<TTx> {
  transaction<T>(fn: (tx: TTx) => Promise<T>): Promise<T>;
}

/**
 * Runs `fn` inside `dbs.length` real, separately-connected transactions.
 *
 * Phase 1 (overlap barrier): every transaction opens (BEGIN completes --
 * Drizzle's `db.transaction()` awaits that before invoking the callback,
 * see drizzle-orm/node-postgres/session.js `NodePgSession.transaction`),
 * signals ready, and parks. Only once every participant has signalled ready
 * does the harness release them to call `fn` simultaneously.
 *
 * Phase 2 (hold barrier): once a claimer's `fn` call succeeds, it signals
 * "holding" and parks again *before* returning (i.e. before its transaction
 * commits), keeping any row lock it took held open. The harness waits until
 * every participant has either signalled "holding" or its whole transaction
 * has already failed (e.g. a blocked lock wait hit `statement_timeout`)
 * before releasing the hold barrier, letting the holders finally commit.
 *
 * Returns each claimer's outcome (fulfilled with its rows, or rejected with
 * whatever error killed its transaction) plus the wall-clock interval each
 * `fn` call took, so the caller can assert genuine overlap with
 * {@link assertGenuineOverlap}.
 */
export async function runConcurrentlyHoldingLocks<TTx, T>(
  dbs: TransactionalDb<TTx>[],
  fn: (tx: TTx) => Promise<T>,
): Promise<ConcurrentRunResult<T>> {
  const n = dbs.length;
  if (n < 2) {
    throw new Error('runConcurrentlyHoldingLocks needs at least 2 participants to prove overlap');
  }

  const readyResolvers: Array<() => void> = [];
  const readyPromises = Array.from(
    { length: n },
    () => new Promise<void>((resolve) => readyResolvers.push(resolve)),
  );
  let releaseEntryGate!: () => void;
  const entryGate = new Promise<void>((resolve) => {
    releaseEntryGate = resolve;
  });

  const holdingResolvers: Array<() => void> = [];
  const holdingSignals = Array.from(
    { length: n },
    () => new Promise<void>((resolve) => holdingResolvers.push(resolve)),
  );
  let releaseHoldGate!: () => void;
  const holdGate = new Promise<void>((resolve) => {
    releaseHoldGate = resolve;
  });

  const intervals: ClaimInterval[] = new Array(n);

  const txPromises = dbs.map((database, i) =>
    database.transaction(async (tx) => {
      // BEGIN has already completed by the time this callback runs --
      // signalling here proves every participant has a live, open
      // transaction before any of them proceeds to claim.
      readyResolvers[i]!();
      await entryGate;

      const start = process.hrtime.bigint();
      try {
        const result = await fn(tx);
        intervals[i] = { start, end: process.hrtime.bigint() };
        // Hold whatever lock this claim just took until every other
        // claimer has also either succeeded (and is holding) or failed --
        // see the module doc for why this is what makes a missing
        // SKIP LOCKED observable instead of accidentally self-healing.
        holdingResolvers[i]!();
        await holdGate;
        return result;
      } catch (err) {
        intervals[i] = { start, end: process.hrtime.bigint() };
        throw err;
      }
    }),
  );

  await Promise.all(readyPromises);
  releaseEntryGate();

  // Every claimer has now either signalled it's holding a successful claim,
  // or its transaction has already failed (e.g. statement_timeout firing on
  // a genuinely blocked lock wait). Only once *all* have reached one of
  // those two states do we let the holders commit.
  await Promise.all(
    txPromises.map((txPromise, i) => Promise.race([holdingSignals[i]!, txPromise.catch(() => undefined)])),
  );
  releaseHoldGate();

  const settled = await Promise.allSettled(txPromises);
  const outcomes: ConcurrentClaimOutcome<T>[] = settled.map((s) =>
    s.status === 'fulfilled' ? { status: 'fulfilled', value: s.value } : { status: 'rejected', error: s.reason },
  );

  return { outcomes, intervals };
}

/**
 * Proves the run above wasn't accidentally serialised: every claimer's
 * [start, end] wall-clock interval (recorded around the `fn` call itself,
 * inside the open transaction) must genuinely overlap the others', i.e. the
 * latest start must precede the earliest end. If a future refactor
 * re-serialises the claimers -- e.g. a pool bottleneck, or reverting this
 * harness back to a bare `Promise.all` over whole transactions -- this
 * throws instead of letting the suite pass quietly on numbers that no
 * longer test concurrency at all.
 */
export function assertGenuineOverlap(intervals: ClaimInterval[]): void {
  if (intervals.some((iv) => iv === undefined)) {
    throw new Error('assertGenuineOverlap: missing interval -- not every claimer recorded one');
  }
  const maxStart = intervals.reduce((max, iv) => (iv.start > max ? iv.start : max), intervals[0]!.start);
  const minEnd = intervals.reduce((min, iv) => (iv.end < min ? iv.end : min), intervals[0]!.end);
  if (!(maxStart < minEnd)) {
    const startsUs = intervals.map((iv) => Number(iv.start) / 1000);
    const endsUs = intervals.map((iv) => Number(iv.end) / 1000);
    throw new Error(
      `expected ${intervals.length} concurrent claimers to genuinely overlap ` +
        `(max start ${Number(maxStart) / 1000}us should be < min end ${Number(minEnd) / 1000}us) ` +
        `-- they ran serially instead. starts(us)=${JSON.stringify(startsUs)} ends(us)=${JSON.stringify(endsUs)}`,
    );
  }
}
