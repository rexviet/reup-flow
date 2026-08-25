/**
 * F4 at the lane -- phase-07 §7.1 and its two headline DoD lines:
 *
 *   "Fake parser 400 + probe OK   -> `failed` with a clear reason, NOT `blocked`"
 *   "Fake parser 400 + probe fail -> `blocked`"
 *
 * `test/parser/f4-probe.spec.ts` proves the probe can tell its five outcomes
 * apart. This file proves the lane ACTS on that: which status is written, how
 * many requests it cost, and -- the part that is easiest to get wrong -- that
 * the consecutive-400 counter resets on success, so a long job with scattered
 * failures is never reported as a cookie problem.
 *
 * WHAT MAKES THE TWO DoD LINES NON-TRIVIAL. Both `blocked` and `failed` are in
 * `RETRYABLE_STATUSES`, so a test that merely walks the flow to the end passes
 * either way -- the shape that has produced a false green on this repo three
 * times (see the note at the top of cookie-recovery.spec.ts). So each assertion
 * here names the status directly AND checks the consequence that follows from
 * it: the reason text an operator reads, and whether the checkpoint survived.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { jobs, profiles, type Job } from '../../src/db/schema';
import { EnumerateLane, type EnumerateParser } from '../../src/lanes/enumerate.lane';
import { ParserRejectedError, ParserUnavailableError } from '../../src/parser/parser.errors';
import type { UserPostPage, UserProfile } from '../../src/parser/parser.client';
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

const SCHEMA = testSchemaName('f4_probe');
const PROFILE = 'MS4wLjABAAAA_f4_target';
const PROBE_ID = 'MS4wLjABAAAA_f4_known_public';
const PAGE_1_CURSOR = 1_735_689_600_000;

let pool: Pool;
let db: ReturnType<typeof testDb>;

/**
 * Records every line, because for one claim in this file THE LOG IS THE ONLY
 * OBSERVABLE. See "the claim can go away while the probe is in flight".
 */
const lines: string[] = [];
const recordingLogger = {
  error: (m: string) => lines.push(m),
  warn: (m: string) => lines.push(m),
  log: (m: string) => lines.push(m),
};

/**
 * A parser that fails the target profile and can answer the probe five
 * different ways.
 *
 * plan §5 rule 9 is the reason both halves are scriptable independently: the
 * whole point of F4 is that the TARGET call looks identical in all four causes,
 * so a fixture where the target's failure carried a hint would be testing a
 * parser this service does not have.
 */
type ProbeOutcome = 'healthy' | 'refused' | 'no-status' | 'fork-400' | 'unreachable';

class ScriptedParser implements EnumerateParser {
  /** Pages to serve, keyed by the cursor that asks for them. Consumed in order. */
  pages: (UserPostPage | 'reject-400')[] = [];
  probeOutcome: ProbeOutcome = 'healthy';
  readonly pageCalls: number[] = [];
  readonly probeCalls: string[] = [];

  async fetchUserPostPage(_secUserId: string, maxCursor: number): Promise<UserPostPage> {
    this.pageCalls.push(maxCursor);
    const next = this.pages.shift();
    if (next === undefined) throw new Error('the script ran out of pages');
    if (next === 'reject-400') {
      throw new ParserRejectedError(400, 'parser refused GET fetch_user_post_videos with HTTP 400');
    }
    return next;
  }

  async fetchUserProfile(secUserId: string): Promise<UserProfile> {
    this.probeCalls.push(secUserId);
    switch (this.probeOutcome) {
      case 'healthy':
        return { nickname: 'a known public account', statusCode: 0 };
      case 'refused':
        return { nickname: null, statusCode: 2190 };
      case 'no-status':
        return { nickname: null, statusCode: null };
      case 'fork-400':
        throw new ParserRejectedError(400, 'parser refused GET handler_user_profile with HTTP 400');
      case 'unreachable':
        throw new ParserUnavailableError('parser did not answer');
    }
  }
}

function page(overrides: Partial<UserPostPage> = {}): UserPostPage {
  return {
    awemeList: [
      {
        aweme_id: `f4-${Math.random().toString(36).slice(2, 10)}`,
        desc: null,
        create_time: 1_700_000_001,
        video: { duration: 1_000, play_addr: { url_list: ['https://cdn.example/x.mp4'] } },
      },
    ],
    hasMore: true,
    maxCursor: PAGE_1_CURSOR,
    statusCode: 0,
    ...overrides,
  };
}

function laneFor(parser: EnumerateParser): EnumerateLane {
  return new EnumerateLane(recordingLogger, db, parser, {
    leaseSeconds: 300,
    urlTtlSeconds: 7200,
    sleepMsMin: 1,
    sleepMsMax: 2,
    probeSecUserId: PROBE_ID,
  });
}

async function readJob(id: string): Promise<Job> {
  const [row] = await db.select().from(jobs).where(eq(jobs.id, id));
  if (!row) throw new Error(`job ${id} not found`);
  return row;
}

async function seedJob(): Promise<string> {
  const [row] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
  return row!.id;
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
  lines.length = 0;
  await db.insert(profiles).values({ secUserId: PROFILE }).onConflictDoNothing();
});

afterEach(async () => {
  await truncateAll(pool);
});

describe('three consecutive HTTP 400s, then the probe decides', () => {
  it('probe OK -> the job is FAILED with a profile-specific reason, never blocked', async () => {
    const parser = new ScriptedParser();
    parser.pages = ['reject-400', 'reject-400', 'reject-400'];
    parser.probeOutcome = 'healthy';
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    const job = await readJob(id);
    // THE DoD LINE. `failed`, because the cookie was proved good -- so nobody
    // is sent to rotate it.
    expect(job.status).toBe('failed');
    expect(job.status).not.toBe('blocked');
    // "kèm lý do rõ": the reason has to say the cookie is fine, or an operator
    // reading only the status still reaches for a new cookie.
    expect(job.error).toMatch(/cookie is still good/i);
    expect(job.error).toMatch(/private, deleted/i);
    // Exactly one probe -- not one per 400.
    expect(parser.probeCalls).toEqual([PROBE_ID]);
    expect(parser.pageCalls).toHaveLength(3);
  });

  it('probe fails on the network -> the job is BLOCKED, and says to wait', async () => {
    const parser = new ScriptedParser();
    parser.pages = ['reject-400', 'reject-400', 'reject-400'];
    parser.probeOutcome = 'unreachable';
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    const job = await readJob(id);
    expect(job.status).toBe('blocked');
    expect(job.error).toMatch(/could not be reached/i);
  });

  it('probe refused by Douyin -> BLOCKED, and says to push a fresh cookie', async () => {
    const parser = new ScriptedParser();
    parser.pages = ['reject-400', 'reject-400', 'reject-400'];
    parser.probeOutcome = 'refused';
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    const job = await readJob(id);
    expect(job.status).toBe('blocked');
    expect(job.error).toMatch(/fresh cookie/i);
  });

  it('probe 400 -> BLOCKED, and names a_bogus rather than the cookie', async () => {
    const parser = new ScriptedParser();
    parser.pages = ['reject-400', 'reject-400', 'reject-400'];
    parser.probeOutcome = 'fork-400';
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    const job = await readJob(id);
    expect(job.status).toBe('blocked');
    expect(job.error).toMatch(/a_bogus/);
  });

  it('an unreadable probe -> BLOCKED, because it ruled nothing out', async () => {
    // The verdict that must NOT drift towards `failed`: `failed` writes the
    // profile off, and an unreadable probe is not evidence for that.
    const parser = new ScriptedParser();
    parser.pages = ['reject-400', 'reject-400', 'reject-400'];
    parser.probeOutcome = 'no-status';
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    expect((await readJob(id)).status).toBe('blocked');
  });

  it('keeps the checkpoint on BOTH verdicts, so /retry resumes rather than re-pages', async () => {
    // The reason `markBlocked` never touches `max_cursor`, applied to the new
    // `failed` path too: a job that gave up on page fourteen must not re-spend
    // the whole F2 budget if the profile comes back.
    for (const outcome of ['healthy', 'refused'] as ProbeOutcome[]) {
      const parser = new ScriptedParser();
      // One good page (so there IS a checkpoint), then the failures.
      parser.pages = [page(), 'reject-400', 'reject-400', 'reject-400'];
      parser.probeOutcome = outcome;
      const id = await seedJob();

      await laneFor(parser).runOnce(fresh());

      const job = await readJob(id);
      expect(job.maxCursor).toBe(PAGE_1_CURSOR);
      expect(job.enumeratedCount).toBe(1);
      // And no lease, so the reaper cannot resurrect a job that is waiting on a
      // human -- the same rule the phase-5 park path follows.
      expect(job.leaseExpiresAt).toBeNull();
      await truncateAll(pool);
      await db.insert(profiles).values({ secUserId: PROFILE }).onConflictDoNothing();
    }
  });
});

describe('the probe costs nothing when nothing is wrong', () => {
  it('never probes on a clean enumerate', async () => {
    // §7.1 tier 3: "probe chỉ chạy trên đường lỗi". A probe on the happy path
    // would be one extra request per job against an IP-based rate limit, which
    // is the budget F2 exists to protect.
    const parser = new ScriptedParser();
    parser.pages = [page({ hasMore: false, maxCursor: PAGE_1_CURSOR })];
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    expect((await readJob(id)).status).toBe('downloading');
    expect(parser.probeCalls).toEqual([]);
  });

  it('never probes on the STRUCTURAL blocked signal, which is already certain', async () => {
    // Empty `aweme_list` + `has_more` is an HTTP 200 and a certain diagnosis
    // (§5.3), so spending a probe on it would be a request bought for an answer
    // already in hand.
    const parser = new ScriptedParser();
    parser.pages = [page({ awemeList: [], hasMore: true })];
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    expect((await readJob(id)).status).toBe('blocked');
    expect(parser.probeCalls).toEqual([]);
  });

  it('does not probe before the third 400', async () => {
    // Two 400s and then a good page: the retries are the cheap remedy, and the
    // probe is only worth its request once they are exhausted.
    const parser = new ScriptedParser();
    parser.pages = ['reject-400', 'reject-400', page({ hasMore: false })];
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    expect(parser.probeCalls).toEqual([]);
    expect((await readJob(id)).status).toBe('downloading');
  });
});

describe('the consecutive-400 counter', () => {
  it('resets on a success, so scattered failures never look like a banned cookie', async () => {
    // The risk register line: "Đếm 400 không reset khi thành công -> job dài sẽ
    // tích đủ 3 lỗi rải rác rồi báo blocked sai." Two 400s, a good page, two
    // more 400s -- five failures' worth of noise, and NOT three in a row, so
    // the job must finish normally and never be diagnosed at all.
    const parser = new ScriptedParser();
    parser.pages = [
      'reject-400',
      'reject-400',
      page({ hasMore: true, maxCursor: PAGE_1_CURSOR }),
      'reject-400',
      'reject-400',
      page({ hasMore: false, maxCursor: PAGE_1_CURSOR - 1_000 }),
    ];
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    expect(parser.probeCalls).toEqual([]);
    const job = await readJob(id);
    expect(job.status).toBe('downloading');
    expect(job.error).toBeNull();
  });

  it('still parks when the third 400 arrives AFTER a success', async () => {
    // The other half of the same claim, and the one that makes the reset above
    // more than "the counter never fires": once three really are consecutive,
    // the diagnosis still happens, whatever came before them.
    const parser = new ScriptedParser();
    parser.pages = [
      page({ hasMore: true, maxCursor: PAGE_1_CURSOR }),
      'reject-400',
      'reject-400',
      'reject-400',
    ];
    parser.probeOutcome = 'refused';
    const id = await seedJob();

    await laneFor(parser).runOnce(fresh());

    expect((await readJob(id)).status).toBe('blocked');
    expect(parser.probeCalls).toEqual([PROBE_ID]);
  });
});

describe('the claim can go away while the probe is in flight', () => {
  it('writes nothing and says so when the job was reaped mid-probe', async () => {
    // THE WINDOW PHASE 7 OPENED. The probe is a network call with a 30s budget
    // and it does NOT renew the lease, so the reaper has a real chance to hand
    // this job to somebody else while we wait for it. `markBlocked`/`markFailed`
    // are gated on `status = 'enumerating'`, so the write then affects no row --
    // and the phase-5 code logged "job X is BLOCKED" regardless, about a job
    // that is `pending` under a new claim. A log line asserting the opposite of
    // what happened is worse than no log line.
    //
    // The reap is performed FROM INSIDE the probe, which is the only place it is
    // the race rather than a re-enactment of it.
    const parser = new ScriptedParser();
    parser.pages = ['reject-400', 'reject-400', 'reject-400'];
    parser.probeOutcome = 'refused';
    const id = await seedJob();
    const stolen: string[] = [];
    const realProbe = parser.fetchUserProfile.bind(parser);
    parser.fetchUserProfile = async (secUserId: string) => {
      await pool.query("UPDATE jobs SET status = 'pending' WHERE id = $1", [id]);
      stolen.push(id);
      return realProbe(secUserId);
    };

    await laneFor(parser).runOnce(fresh());

    expect(stolen).toEqual([id]);
    const job = await readJob(id);
    // NOT blocked: the verdict was reached but the row was not ours to write.
    expect(job.status).toBe('pending');
    expect(job.error).toBeNull();

    // AND THIS IS THE PART THAT MAKES THE TEST ABLE TO GO RED AT ALL.
    //
    // The two database assertions above are true WITH OR WITHOUT the fix: the
    // write is gated on `status = 'enumerating'` either way, so ignoring its
    // answer changes no row. The only observable difference is what the lane
    // SAYS -- and the whole defect was a log line claiming "job X is BLOCKED"
    // about a job that is `pending` under somebody else's claim. So the log is
    // the assertion, not decoration on one. (Caught by the mutation suite: the
    // first version of this test stayed green under the mutation it exists for.)
    const said = lines.join('\n');
    expect(said).not.toMatch(/is BLOCKED/);
    expect(said).toMatch(/the claim was already gone/i);
  });
});

describe('errors that are not a Douyin verdict', () => {
  it('lets a non-400 rejection out of the tick instead of diagnosing it', async () => {
    // 404/422 from the page route means we and the fork disagree about the
    // route. Retrying cannot fix it and probing would spend a request to learn
    // nothing, so the tick fails and the base class backs off.
    const parser: EnumerateParser = {
      fetchUserPostPage: async () => {
        throw new ParserRejectedError(422, 'parser refused with HTTP 422');
      },
      fetchUserProfile: async () => {
        throw new Error('the probe must not be called for a 422');
      },
    };
    const id = await seedJob();

    await expect(laneFor(parser).runOnce(fresh())).rejects.toThrow(/422/);

    // And the claim went back, so the next tick can retry it.
    expect((await readJob(id)).status).toBe('pending');
  });
});
