/**
 * Plan §5's mutation row: "`blocked` ≠ `failed` -- map `blocked` → `failed` --
 * test hồi phục cookie ĐỎ". Plan §9's DoD line: "Cookie hết hạn giữa job →
 * `blocked` (không `failed`) → push cookie mới → `/retry` → tiếp từ checkpoint".
 *
 * WHAT THIS TEST HAD TO WORK AROUND, stated up front because the obvious
 * version of it is a false green.
 *
 * `RETRYABLE_STATUSES` in jobs.repository.ts is `['blocked', 'failed']`. So a
 * recovery test that only walks the flow -- get stuck, push a cookie, retry,
 * finish -- PASSES UNCHANGED with `blocked` mapped to `failed`. It would look
 * like strong end-to-end evidence and prove nothing about the distinction it
 * is named after. (This repo has shipped that shape three times; see
 * `.claude/agent-memory/.../false-greens-reup-flow.md`.)
 *
 * So the distinction is asserted through the two places it is actually
 * observable today:
 *
 *   1. THE WORD ITSELF. Both statuses are retryable, so the status IS the
 *      remediation instruction: `blocked` means "rotate the cookie and this
 *      job continues", `failed` means "this profile is not gettable and a new
 *      cookie changes nothing". Getting it wrong does not break the machine,
 *      it sends a person to do the wrong thing -- which is exactly the cost
 *      F4 exists to avoid, and is why it is worth a test at all.
 *   2. CANCELLABILITY. `CANCELLABLE_STATUSES` includes `blocked` and excludes
 *      `failed`, because a blocked job is live-but-stuck while a failed one is
 *      finished. That is a behavioural consequence of the word, already
 *      shipped in phase 3, and it cannot be satisfied by a job that was marked
 *      `failed`.
 *
 * Phase 7 adds the third and biggest one: the F4 probe, which is what will
 * route a private profile to `failed` while keeping a banned cookie at
 * `blocked`.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { cancel, retry } from '../../src/db/jobs.repository';
import { jobs, profiles, videos, type Job } from '../../src/db/schema';
import { EnumerateLane, type PostPageSource } from '../../src/lanes/enumerate.lane';
import type { UserPostPage } from '../../src/parser/parser.client';
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

const SCHEMA = testSchemaName('cookie_recovery');
const PROFILE = 'MS4wLjABAAAA_recovery';
const PAGE_1_CURSOR = 1_735_689_600_000;
const PAGE_2_CURSOR = 1_735_603_200_000;

let pool: Pool;
let db: ReturnType<typeof testDb>;

const quietLogger = { error: () => {}, warn: () => {}, log: () => {} };

/**
 * A page source whose cookie can be "revoked" and "rotated" mid-test.
 *
 * While revoked it answers the way a refused cookie really does: HTTP 200,
 * `aweme_list: []`, `has_more: 1`. That is the structural signal of §5.3 -- a
 * certain diagnosis, which is why phase 5 acts on it with no probe.
 */
class RotatableCookieSource implements PostPageSource {
  revoked = false;
  /** Revoke the cookie once this many pages have been served. */
  revokeAfter = Number.POSITIVE_INFINITY;
  readonly cursors: number[] = [];

  constructor(private readonly pagesByCursor: Map<number, UserPostPage>) {}

  async fetchUserPostPage(_secUserId: string, maxCursor: number): Promise<UserPostPage> {
    this.cursors.push(maxCursor);
    if (this.revoked) {
      return { awemeList: [], hasMore: true, maxCursor, statusCode: 0 };
    }
    const page = this.pagesByCursor.get(maxCursor);
    if (!page) throw new Error(`no scripted page for cursor ${maxCursor}`);
    if (this.cursors.length >= this.revokeAfter) this.revoked = true;
    return page;
  }
}

function item(id: string, when: number): Record<string, unknown> {
  return {
    aweme_id: id,
    desc: `clip ${id}`,
    create_time: when,
    video: { duration: 9_000, play_addr: { url_list: [`https://cdn.example/${id}.mp4`] } },
  };
}

function laneFor(source: PostPageSource): EnumerateLane {
  return new EnumerateLane(quietLogger, db, source, {
    leaseSeconds: 300,
    urlTtlSeconds: 7200,
    sleepMsMin: 1,
    sleepMsMax: 2,
  });
}

async function readJob(id: string): Promise<Job> {
  const [row] = await db.select().from(jobs).where(eq(jobs.id, id));
  if (!row) throw new Error(`job ${id} not found`);
  return row;
}

const fresh = () => new AbortController().signal;

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
  await db.insert(profiles).values({ secUserId: PROFILE }).onConflictDoNothing();
});

afterEach(async () => {
  await truncateAll(pool);
});

/** Two pages, keyed by the cursor that asks for them. */
function script(): Map<number, UserPostPage> {
  return new Map<number, UserPostPage>([
    [
      0,
      {
        awemeList: [item('rec-1', 1_700_000_001), item('rec-2', 1_700_000_002)],
        hasMore: true,
        maxCursor: PAGE_1_CURSOR,
        statusCode: 0,
      },
    ],
    [
      PAGE_1_CURSOR,
      {
        awemeList: [item('rec-3', 1_700_000_003)],
        hasMore: false,
        maxCursor: PAGE_2_CURSOR,
        statusCode: 0,
      },
    ],
  ]);
}

describe('a cookie that is banned mid-job', () => {
  it('recovers end to end: blocked -> new cookie -> retry -> resume from the checkpoint', async () => {
    const [seeded] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    const id = seeded!.id;

    // Page one lands, and the cookie is banned before page two is asked for.
    const source = new RotatableCookieSource(script());
    source.revokeAfter = 1;

    await laneFor(source).runOnce(fresh());

    const stuck = await readJob(id);
    // (1) THE WORD. Not `failed`: this is the instruction to rotate the cookie.
    expect(stuck.status).toBe('blocked');
    // Page one's work survived, which is what makes the recovery cheap.
    expect(stuck.maxCursor).toBe(PAGE_1_CURSOR);
    expect(stuck.enumeratedCount).toBe(2);
    expect(await db.select().from(videos).where(eq(videos.secUserId, PROFILE))).toHaveLength(2);

    // The operator pushes a fresh cookie and hits /retry.
    expect(await retry(db, id)).not.toBeNull();
    expect((await readJob(id)).status).toBe('pending');
    // The stale reason is cleared -- a `blocked` explanation outliving the
    // problem it described is how an operator ends up chasing a fixed bug.
    expect((await readJob(id)).error).toBeNull();

    const resumed = new RotatableCookieSource(script());
    await laneFor(resumed).runOnce(fresh());

    // Resumed AT THE CHECKPOINT: page one was never re-fetched, which is the
    // F2 request budget the whole `blocked`-not-`failed` choice exists to save.
    expect(resumed.cursors).toEqual([PAGE_1_CURSOR]);
    const done = await readJob(id);
    expect(done.status).toBe('downloading');
    expect(done.totalVideos).toBe(3);
  });

  it('leaves the parked job CANCELLABLE, the way a live-but-stuck job must be', async () => {
    // (2) The behavioural half. `CANCELLABLE_STATUSES` includes `blocked` and
    // EXCLUDES `failed`, because a blocked job is waiting on an operator while a
    // failed one is over. So this is a consequence of the word rather than a
    // restatement of it -- a job wrongly marked `failed` cannot satisfy it.
    //
    // `cancel` is called FIRST, before any assertion about the status. The
    // earlier version asserted `toBe('blocked')` first, so under the very
    // mutation this test exists for (`blocked` -> `failed`) it died on that line
    // and never reached the cancel -- leaving the file with two assertions of
    // the word and no independent behavioural evidence at all. Ordering is what
    // makes the second mechanism load-bearing instead of decorative.
    const source = new RotatableCookieSource(script());
    source.revoked = true;
    const [seeded] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();

    await laneFor(source).runOnce(fresh());

    expect(await cancel(db, seeded!.id)).not.toBeNull();
    expect((await readJob(seeded!.id)).status).toBe('cancelled');
  });

  it('holds no lease once parked, so the reaper cannot resurrect it', async () => {
    // A parked job is waiting on a human, not on a worker. A stale lease left
    // behind would let the reaper "recover" a job nobody is enumerating, putting
    // it back to `pending` so the lane retries against the same dead cookie --
    // in a loop, forever, erasing the operator's signal on every pass.
    //
    // Retitled: the previous title also claimed this covered "not consuming an
    // enumerate attempt", and there is no such thing to consume -- `jobs` has no
    // `attempts` column at all (F6's `attempts` is on `videos`). A title naming
    // a field the schema has never had is the same drift as an assertion naming
    // one, and it reads as coverage that does not exist.
    const source = new RotatableCookieSource(script());
    source.revoked = true;
    const [seeded] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();

    await laneFor(source).runOnce(fresh());

    expect((await readJob(seeded!.id)).leaseExpiresAt).toBeNull();
    // And the reaper's own precondition is genuinely absent, not merely null in
    // a column nobody reads: no `enumerating` row means nothing for F5 to sweep.
    expect((await readJob(seeded!.id)).status).not.toBe('enumerating');
  });
});
