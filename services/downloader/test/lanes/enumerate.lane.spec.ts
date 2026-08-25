/**
 * Phase 5 -- the enumerate lane.
 *
 * Driven through `runOnce()` called by hand against a real Postgres and a
 * scripted page source. No scheduler, no Nest container: that is reason 3 for
 * choosing Approach 1 (solution design §4), and it is what makes this phase's
 * mutations cheap to write.
 *
 * The pacing numbers are COMPRESSED (tens of ms instead of 3-5s) and everything
 * else is the real thing -- the real lane, the real repository functions, real
 * SQL. A suite that slept 3 seconds per page would take minutes and prove
 * nothing extra; what it must not do is compress the SHAPE, which is why the
 * timing assertions below measure the gap BETWEEN CONSECUTIVE REQUESTS rather
 * than the total elapsed time. Total time is satisfied by a single sleep
 * anywhere -- including one hoisted out of the loop, which is the exact
 * regression phase-05's risk register names.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { jobs, profiles, videos, type Job, type Video } from '../../src/db/schema';
import {
  ENUMERATE_IDLE_MS,
  ENUMERATE_PAGE_SIZE,
  EnumerateLane,
  MAX_CONSECUTIVE_REJECTIONS,
  type EnumerateParser,
} from '../../src/lanes/enumerate.lane';
import { ParserRejectedError, ParserUnavailableError } from '../../src/parser/parser.errors';
import type { Aweme, UserPostPage, UserProfile } from '../../src/parser/parser.client';
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

const SCHEMA = testSchemaName('enumerate');
const PROFILE = 'MS4wLjABAAAA_enumerate';
const LEASE_SECONDS = 300;
const URL_TTL_SECONDS = 7200;
/** The F4 probe target. Any value: no test here lets a probe reach a network. */
const PROBE_SEC_USER_ID = 'MS4wLjABAAAA_probe_target';
/** Compressed pacing. The SHAPE (a sleep between pages) is not compressed. */
const SLEEP_MIN_MS = 60;
const SLEEP_MAX_MS = 90;

let pool: Pool;
let db: ReturnType<typeof testDb>;
const logged: string[] = [];

const recordingLogger = {
  error: (m: string) => logged.push(`ERROR ${m}`),
  warn: (m: string) => logged.push(`WARN ${m}`),
  log: (m: string) => logged.push(`LOG ${m}`),
};

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
  logged.length = 0;
  await db.insert(profiles).values({ secUserId: PROFILE }).onConflictDoNothing();
});

afterEach(async () => {
  await truncateAll(pool);
});

/* -------------------------------------------------------------------------- */
/* Fixtures                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * Douyin's cursor is the create_time (ms) of the oldest item in the page, and a
 * profile pages NEWEST FIRST -- so the scripted cursors below DESCEND, exactly
 * as the real thing does. Ascending cursors would have hidden the `greatest()`
 * bug in `checkpoint` (see jobs.repository.ts) behind a green suite.
 */
const PAGE_1_CURSOR = 1_735_689_600_000;
const PAGE_2_CURSOR = 1_735_603_200_000;
const PAGE_3_CURSOR = 1_735_516_800_000;

let awemeSeq = 0;

function aweme(): Aweme {
  awemeSeq += 1;
  return {
    aweme_id: `aweme-${awemeSeq}`,
    desc: `clip ${awemeSeq}`,
    create_time: 1_700_000_000 + awemeSeq,
    video: {
      duration: 12_000,
      play_addr: { url_list: [`https://cdn.example/${awemeSeq}.mp4`] },
    },
  };
}

function page(count: number, maxCursor: number, hasMore: boolean): UserPostPage {
  return {
    awemeList: Array.from({ length: count }, () => aweme()),
    hasMore,
    maxCursor,
    statusCode: 0,
  };
}

/** A page that carries no items but claims there are more -- the F2/blocked signal. */
function blockedPage(): UserPostPage {
  return { awemeList: [], hasMore: true, maxCursor: PAGE_2_CURSOR, statusCode: 0 };
}

interface Call {
  secUserId: string;
  maxCursor: number;
  count: number;
  at: number;
}

type Script = (UserPostPage | Error)[];

/**
 * A scripted `fetchUserPostPage`, recording every call and WHEN it happened.
 *
 * `beforeCall` runs before the answer is produced, which is how the
 * checkpoint-per-page tests observe the database mid-enumerate. `holdMs` keeps
 * a call in flight so overlap between two lanes is observable at all -- see
 * enumerate-global-concurrency.spec.ts.
 */
class ScriptedSource implements EnumerateParser {
  readonly calls: Call[] = [];
  beforeCall?: (index: number) => Promise<void> | void;
  holdMs = 0;
  /**
   * What the phase-7 F4 probe gets back, and `null` -- the default -- means
   * "THIS TEST ASSERTS THE PROBE IS NEVER CALLED".
   *
   * Deliberately not a benign stub. A stub that quietly answered would turn
   * every test in this file into one that cannot notice a probe being spent on
   * the happy path, and a probe on the happy path is one extra request per job
   * against an IP-based rate limit (F2). plan §5's memory entry for this shape:
   * "a neutralised seam is a vacuous test".
   */
  probeAnswer: UserProfile | Error | null = null;
  readonly probeCalls: string[] = [];

  constructor(private readonly script: Script) {}

  async fetchUserProfile(secUserId: string): Promise<UserProfile> {
    this.probeCalls.push(secUserId);
    if (this.probeAnswer === null) {
      throw new Error(
        'the F4 probe was called by a test that did not script one -- either the lane is ' +
          'probing on a path that must not cost a request, or this test needs probeAnswer set',
      );
    }
    if (this.probeAnswer instanceof Error) throw this.probeAnswer;
    return this.probeAnswer;
  }

  async fetchUserPostPage(
    secUserId: string,
    maxCursor: number,
    count: number,
  ): Promise<UserPostPage> {
    const index = this.calls.length;
    this.calls.push({ secUserId, maxCursor, count, at: Date.now() });
    await this.beforeCall?.(index);
    if (this.holdMs) await new Promise((r) => setTimeout(r, this.holdMs));
    const next = this.script[index];
    if (next === undefined) throw new Error(`script exhausted at call ${index}`);
    if (next instanceof Error) throw next;
    return next;
  }
}

function laneFor(source: EnumerateParser, overrides: Record<string, unknown> = {}): EnumerateLane {
  return new EnumerateLane(recordingLogger, db, source, {
    leaseSeconds: LEASE_SECONDS,
    urlTtlSeconds: URL_TTL_SECONDS,
    sleepMsMin: SLEEP_MIN_MS,
    sleepMsMax: SLEEP_MAX_MS,
    probeSecUserId: PROBE_SEC_USER_ID,
    ...overrides,
  });
}

async function seedJob(overrides: Partial<Job> = {}): Promise<string> {
  const [row] = await db
    .insert(jobs)
    .values({ secUserId: PROFILE, ...overrides })
    .returning();
  return row!.id;
}

async function readJob(id: string): Promise<Job> {
  const [row] = await db.select().from(jobs).where(eq(jobs.id, id));
  if (!row) throw new Error(`job ${id} not found`);
  return row;
}

async function allVideos(): Promise<Video[]> {
  return db.select().from(videos).where(eq(videos.secUserId, PROFILE));
}

const fresh = () => new AbortController().signal;

/* -------------------------------------------------------------------------- */

describe('the lane itself', () => {
  it('is named so the staleness alert can name it, and polls at 1s when idle', () => {
    const lane = laneFor(new ScriptedSource([]));
    expect(lane.name).toBe('enumerate');
    expect(lane.idleDelayMs).toBe(ENUMERATE_IDLE_MS);
    expect(ENUMERATE_IDLE_MS).toBe(1_000);
  });

  it('reports idle -- and issues NO parser request -- when there is no job', async () => {
    // The request that matters most is the one not made. An idle lane that
    // still called Douyin once per second would burn the IP-based rate limit
    // (F2) doing nothing at all.
    const source = new ScriptedSource([]);

    await expect(laneFor(source).runOnce(fresh())).resolves.toBe('idle');

    expect(source.calls).toHaveLength(0);
  });

  it('asks for 20 items a page', () => {
    expect(ENUMERATE_PAGE_SIZE).toBe(20);
  });
});

describe('DoD: a ~30-video profile', () => {
  it('enumerates to the end, stores every row, and moves the job to downloading', async () => {
    const id = await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(10, PAGE_2_CURSOR, false),
    ]);

    await expect(laneFor(source).runOnce(fresh())).resolves.toBe('worked');

    const job = await readJob(id);
    expect(job.status).toBe('downloading');
    // `total_videos` must agree with the rows that exist, not with the page
    // sizes the lane added up -- see finishEnumerate.
    expect(job.totalVideos).toBe(30);
    expect(await allVideos()).toHaveLength(30);
    expect(job.enumeratedCount).toBe(30);
    expect(job.leaseExpiresAt).toBeNull();
  });

  it('starts from cursor 0 and follows Douyin`s cursor on every later page', async () => {
    await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(20, PAGE_2_CURSOR, true),
      page(5, PAGE_3_CURSOR, false),
    ]);

    await laneFor(source).runOnce(fresh());

    expect(source.calls.map((c) => c.maxCursor)).toEqual([0, PAGE_1_CURSOR, PAGE_2_CURSOR]);
    expect(source.calls.map((c) => c.count)).toEqual([20, 20, 20]);
    expect(source.calls.every((c) => c.secUserId === PROFILE)).toBe(true);
  });

  it('writes play_addr and url_expires_at IMMEDIATELY after enumerate (F1)', async () => {
    // The whole saving. If these were left for the download lane to resolve,
    // a 500-video profile would cost ~525 requests instead of ~25.
    await seedJob();

    await laneFor(new ScriptedSource([page(3, PAGE_1_CURSOR, false)])).runOnce(fresh());

    for (const row of await allVideos()) {
      expect(row.playAddr).toMatch(/^https:\/\/cdn\.example\//);
      expect(row.urlExpiresAt).not.toBeNull();
      expect(row.urlExpiresAt!.getTime()).toBeGreaterThan(Date.now());
    }
  });

  it('re-running the same profile inserts nothing new (ON CONFLICT DO NOTHING)', async () => {
    await seedJob();
    const first = [page(20, PAGE_1_CURSOR, true), page(10, PAGE_2_CURSOR, false)];
    await laneFor(new ScriptedSource(first)).runOnce(fresh());
    const before = await allVideos();

    // A second job over the same profile, served the same two pages.
    const secondId = await seedJob();
    const replay = new ScriptedSource([
      { ...first[0]!, awemeList: before.slice(0, 20).map(asAweme) },
      { ...first[1]!, awemeList: before.slice(20).map(asAweme) },
    ]);
    await laneFor(replay).runOnce(fresh());

    expect(await allVideos()).toHaveLength(30);
    // Still reaches `downloading` with the right total -- "nothing new" is not
    // the same as "nothing happened".
    const job = await readJob(secondId);
    expect(job.status).toBe('downloading');
    expect(job.totalVideos).toBe(30);
  });
});

/** Rebuilds a Douyin item from a stored row, for the re-run test above. */
function asAweme(row: Video): Aweme {
  return {
    aweme_id: row.awemeId,
    desc: row.desc,
    create_time: Math.floor(row.createTime.getTime() / 1000),
    video: { duration: row.durationMs, play_addr: { url_list: [row.playAddr] } },
  };
}

describe('checkpoint EVERY page, not at the end of the loop', () => {
  it('has already written page one`s cursor and count before page two is asked for', async () => {
    // The assertion that the mutation "move the checkpoint after the loop"
    // makes red. Observed from inside the parser call, which is the only
    // vantage point from which "already" means anything.
    const id = await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(20, PAGE_2_CURSOR, true),
      page(1, PAGE_3_CURSOR, false),
    ]);
    const seen: Pick<Job, 'maxCursor' | 'enumeratedCount'>[] = [];
    source.beforeCall = async (index) => {
      const row = await readJob(id);
      seen.push({ maxCursor: row.maxCursor, enumeratedCount: row.enumeratedCount });
      void index;
    };

    await laneFor(source).runOnce(fresh());

    expect(seen).toEqual([
      { maxCursor: 0, enumeratedCount: 0 },
      { maxCursor: PAGE_1_CURSOR, enumeratedCount: 20 },
      { maxCursor: PAGE_2_CURSOR, enumeratedCount: 40 },
    ]);
  });

  it('renews the lease on every page, so a long enumerate is never reaped out from under itself', async () => {
    const id = await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(1, PAGE_2_CURSOR, false),
    ]);
    const leases: (number | null)[] = [];
    source.beforeCall = async () => {
      leases.push((await readJob(id)).leaseExpiresAt?.getTime() ?? null);
      // Age the lease to the point of expiry between pages. Without a renewal
      // per page, the reaper would be entitled to take this job away mid-flight.
      await pool.query(
        "UPDATE jobs SET lease_expires_at = now() + interval '1 second' WHERE id = $1",
        [id],
      );
    };

    await laneFor(source).runOnce(fresh());

    // Page 2's observation is AFTER page 1's checkpoint, so it must be far in
    // the future again rather than the 1-second value just written.
    expect(leases[1]!).toBeGreaterThan(Date.now() + 60_000);
  });

  it('keeps the checkpoint when the run dies partway, and resumes from it (DoD: no re-scan from 0)', async () => {
    const id = await seedJob();
    const dying = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(20, PAGE_2_CURSOR, true),
      new ParserUnavailableError('parser fell over on page three'),
    ]);

    await expect(laneFor(dying).runOnce(fresh())).rejects.toThrow(/page three/);

    const midway = await readJob(id);
    expect(midway.maxCursor).toBe(PAGE_2_CURSOR);
    expect(midway.enumeratedCount).toBe(40);
    // Handed straight back rather than left for the reaper: the next tick can
    // claim it now, instead of after a full LEASE_SECONDS.
    expect(midway.status).toBe('pending');

    // ... and the restart picks up where it left off.
    const resumed = new ScriptedSource([page(3, PAGE_3_CURSOR, false)]);
    await laneFor(resumed).runOnce(fresh());

    expect(resumed.calls[0]!.maxCursor).toBe(PAGE_2_CURSOR);
    const done = await readJob(id);
    expect(done.status).toBe('downloading');
    expect(done.enumeratedCount).toBe(43);
    // 40 rows from the first run plus page three's 3 -- and, the point, the
    // resumed run made ONE request, not three. The first two pages were never
    // re-fetched, which is the request budget the checkpoint exists to save.
    expect(resumed.calls).toHaveLength(1);
    expect(await allVideos()).toHaveLength(43);
  });
});

describe('a sleep BETWEEN PAGES, inside the loop (F2)', () => {
  it('leaves at least sleepMsMin between consecutive requests', async () => {
    // Measured as GAPS, deliberately. A total-elapsed-time assertion is
    // satisfied by one sleep anywhere -- including one hoisted out of the loop,
    // which is the regression phase-05's risk register names.
    await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(20, PAGE_2_CURSOR, true),
      page(20, PAGE_3_CURSOR, true),
      page(1, PAGE_3_CURSOR - 1000, false),
    ]);

    await laneFor(source).runOnce(fresh());

    const gaps = source.calls.slice(1).map((c, i) => c.at - source.calls[i]!.at);
    expect(gaps).toHaveLength(3);
    for (const gap of gaps) expect(gap).toBeGreaterThanOrEqual(SLEEP_MIN_MS);
  });

  it('jitters the delay rather than issuing a metronome', async () => {
    // A fixed interval is a fingerprint, which is the whole reason the delay is
    // a range rather than a constant.
    //
    // The first version of this test injected `random: () => 0`, which pins the
    // draw to the FLOOR on every page -- it produced a metronome and then
    // asserted that a metronome was within bounds. Proven unfalsifiable by
    // mutation during review: replacing the jitter with a fixed 3000ms left it
    // green. The seam is now used to force draws that DIFFER, and the assertion
    // compares them to each other rather than to the bounds alone.
    const drawn: number[] = [];
    await seedJob();
    // Six pages, so five delays -- enough that "they are not all the same" is a
    // meaningful claim.
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(20, PAGE_1_CURSOR - 1_000, true),
      page(20, PAGE_1_CURSOR - 2_000, true),
      page(20, PAGE_1_CURSOR - 3_000, true),
      page(20, PAGE_1_CURSOR - 4_000, true),
      page(1, PAGE_1_CURSOR - 5_000, false),
    ]);
    // A deterministic stand-in for Math.random that sweeps the range, so the
    // test cannot flake on an unlucky sequence of equal draws.
    let tick = 0;
    const lane = laneFor(source, { random: () => ((tick += 1) % 5) / 5 });
    lane.onDelay = (ms: number) => drawn.push(ms);

    await lane.runOnce(fresh());

    expect(drawn).toHaveLength(5);
    // THE assertion: the delay actually varies. A constant fails here.
    expect(new Set(drawn).size).toBeGreaterThan(1);
    // And it stays inside the configured band, so the request budget is still
    // predictable.
    for (const ms of drawn) {
      expect(ms).toBeGreaterThanOrEqual(SLEEP_MIN_MS);
      expect(ms).toBeLessThanOrEqual(SLEEP_MAX_MS);
    }
  });

  it('paces the retry after an HTTP 400 too', async () => {
    // The path every gap assertion above missed, because none of their scripts
    // contains a 400. Without a delay here the lane fires
    // MAX_CONSECUTIVE_REJECTIONS requests back-to-back at whatever rate the
    // parser can refuse them, parks the job, and -- because the tick still
    // reports `worked` -- comes straight back for the next job and does it
    // again. Under a parser-wide failure that is 3 x queue depth unpaced
    // requests against an IP-based rate limit: the ban F2 exists to prevent,
    // arriving down the one route the pacing tests did not cover.
    await seedJob();
    const source = new ScriptedSource([
      new ParserRejectedError(400, 'refused'),
      new ParserRejectedError(400, 'refused'),
      page(1, PAGE_1_CURSOR, false),
    ]);

    await laneFor(source).runOnce(fresh());

    expect(source.calls).toHaveLength(3);
    const gaps = source.calls.slice(1).map((c, i) => c.at - source.calls[i]!.at);
    for (const gap of gaps) expect(gap).toBeGreaterThanOrEqual(SLEEP_MIN_MS);
  });

  it('does NOT sleep after the final page', async () => {
    // A sleep after the last page is pure latency: it delays the job reaching
    // `downloading` for nothing, since no request follows it.
    await seedJob();
    const source = new ScriptedSource([page(3, PAGE_1_CURSOR, false)]);
    const lane = laneFor(source, { sleepMsMin: 4_000, sleepMsMax: 4_100 });

    const started = Date.now();
    await lane.runOnce(fresh());

    expect(Date.now() - started).toBeLessThan(2_000);
  });
});

describe('blocked -- structural signal only (§5.3)', () => {
  it('reports blocked, NOT failed, when a page is empty but claims there is more', async () => {
    // HTTP 200 with `aweme_list: []` and `has_more: 1` is a CERTAIN diagnosis
    // (the cookie is being refused), which is why it needs no probe. `blocked`
    // rather than `failed` is the entire remediation signal: both are
    // retryable, so the word is what tells an operator to rotate the cookie
    // instead of writing the profile off.
    const id = await seedJob({ maxCursor: PAGE_1_CURSOR, enumeratedCount: 20 });
    const source = new ScriptedSource([blockedPage()]);

    await expect(laneFor(source).runOnce(fresh())).resolves.toBe('worked');

    const job = await readJob(id);
    expect(job.status).toBe('blocked');
    expect(job.error).toMatch(/blocked|cookie/i);
  });

  it('leaves max_cursor exactly where it was', async () => {
    // What makes recovery cheap: a fresh cookie plus `/retry` resumes here
    // instead of re-paging the profile and spending the F2 budget again.
    const id = await seedJob({ maxCursor: PAGE_1_CURSOR, enumeratedCount: 20 });

    await laneFor(new ScriptedSource([blockedPage()])).runOnce(fresh());

    const job = await readJob(id);
    expect(job.maxCursor).toBe(PAGE_1_CURSOR);
    expect(job.enumeratedCount).toBe(20);
  });

  it('stops paging the moment it sees the signal', async () => {
    const source = new ScriptedSource([blockedPage(), page(20, PAGE_3_CURSOR, false)]);
    await seedJob();

    await laneFor(source).runOnce(fresh());

    expect(source.calls).toHaveLength(1);
  });

  it('treats an empty page with has_more UNSET as a finished profile, not as blocked', async () => {
    // The pairing is the signal, not the emptiness. A profile whose item count
    // is an exact multiple of the page size ends on a genuinely empty last
    // page, and calling that a banned cookie would send an operator to rotate a
    // perfectly good one -- the F4 mistake, arrived at from the other side.
    const id = await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      { awemeList: [], hasMore: false, maxCursor: PAGE_2_CURSOR, statusCode: 0 },
    ]);

    await laneFor(source).runOnce(fresh());

    const job = await readJob(id);
    expect(job.status).toBe('downloading');
    expect(job.totalVideos).toBe(20);
  });
});

describe('HTTP 400 -- counted, never diagnosed (§5.3, deferred to phase 7)', () => {
  const rejected = () => new ParserRejectedError(400, 'parser refused with HTTP 400');

  it('retries a 400 and carries on when the next page succeeds', async () => {
    // One 400 is not evidence of anything. The fork answers 400 for every
    // internal exception (F4), so a single one may be a network blip.
    const id = await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      rejected(),
      page(2, PAGE_2_CURSOR, false),
    ]);

    await laneFor(source).runOnce(fresh());

    expect((await readJob(id)).status).toBe('downloading');
    // The retry re-asks for the SAME page, not the next one.
    expect(source.calls.map((c) => c.maxCursor)).toEqual([0, PAGE_1_CURSOR, PAGE_1_CURSOR]);
  });

  it('stops after three consecutive 400s, keeping the checkpoint', async () => {
    // Phase 5 owned the COUNTING; phase 7 owns the verdict. What survives from
    // phase 5 unchanged is what this test asserts: three consecutive 400s end
    // the tick, the checkpoint is kept, and the same page was re-asked exactly
    // three times. WHICH status is written is now the probe's answer, and that
    // is test/lanes/f4-probe.spec.ts's subject -- so this scripts a probe that
    // makes the outcome deterministic rather than restating it.
    expect(MAX_CONSECUTIVE_REJECTIONS).toBe(3);
    const id = await seedJob({ maxCursor: PAGE_1_CURSOR, enumeratedCount: 20 });
    const source = new ScriptedSource([rejected(), rejected(), rejected()]);
    source.probeAnswer = { nickname: null, statusCode: 2190 };

    await expect(laneFor(source).runOnce(fresh())).resolves.toBe('worked');

    const job = await readJob(id);
    expect(job.status).toBe('blocked');
    expect(job.maxCursor).toBe(PAGE_1_CURSOR);
    // The reason is no longer "cause unknown" -- the probe is the observation
    // that removed the unknown. It must name a remediation.
    expect(job.error).toMatch(/fresh cookie/i);
    expect(source.calls).toHaveLength(3);
    // ONE probe for three 400s, not one per 400.
    expect(source.probeCalls).toEqual([PROBE_SEC_USER_ID]);
  });

  it('resets the 400 counter on any successful page', async () => {
    // Consecutive, not cumulative. A profile that throws one 400 every ten
    // pages is unhealthy but not blocked, and stopping it at page thirty would
    // be wrong.
    const id = await seedJob();
    const source = new ScriptedSource([
      rejected(),
      rejected(),
      page(20, PAGE_1_CURSOR, true),
      rejected(),
      rejected(),
      page(1, PAGE_2_CURSOR, false),
    ]);

    await laneFor(source).runOnce(fresh());

    expect((await readJob(id)).status).toBe('downloading');
  });

  it('does NOT treat a non-400 refusal as a cookie problem', async () => {
    // A 404 or 422 means we and the fork disagree about the route or its
    // parameters -- a bug on this side. Reporting it as `blocked` sends an
    // operator to rotate a cookie that was never the problem.
    await seedJob();
    const source = new ScriptedSource([new ParserRejectedError(422, 'wrong parameter names')]);

    await expect(laneFor(source).runOnce(fresh())).rejects.toThrow(/wrong parameter names/);
  });
});

describe('letting go of a claim', () => {
  it('hands the job back on shutdown, with the checkpoint intact (§4.2)', async () => {
    const id = await seedJob();
    const aborter = new AbortController();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(20, PAGE_2_CURSOR, true),
    ]);
    source.beforeCall = (index) => {
      if (index === 1) aborter.abort();
    };
    const lane = laneFor(source);

    await lane.runOnce(aborter.signal);
    // `stop()` is what releases held work -- the same path SIGTERM takes.
    await lane.stop();

    const job = await readJob(id);
    expect(job.status).toBe('pending');
    expect(job.maxCursor).toBe(PAGE_2_CURSOR);
    expect(job.leaseExpiresAt).toBeNull();
  });

  it('stops paging as soon as the shutdown signal is raised', async () => {
    await seedJob();
    const aborter = new AbortController();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(20, PAGE_2_CURSOR, true),
      page(20, PAGE_3_CURSOR, true),
    ]);
    source.beforeCall = (index) => {
      if (index === 0) aborter.abort();
    };

    await laneFor(source).runOnce(aborter.signal);

    expect(source.calls).toHaveLength(1);
  });

  it('stops -- and does not release -- once the job is no longer ours', async () => {
    // A checkpoint that writes no row means the job was reaped or cancelled
    // while we were paging. It may already belong to another lane, so the one
    // thing this must not do is hand it back underneath them.
    const id = await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(20, PAGE_2_CURSOR, true),
    ]);
    source.beforeCall = async (index) => {
      if (index === 0) await pool.query("UPDATE jobs SET status = 'cancelled' WHERE id = $1", [id]);
    };
    const lane = laneFor(source);

    await expect(lane.runOnce(fresh())).resolves.toBe('worked');
    await lane.stop();

    expect(source.calls).toHaveLength(1);
    expect((await readJob(id)).status).toBe('cancelled');
  });

  it('hands the job back when the tick fails, and still reports the failure upward', async () => {
    // Both halves matter. Releasing means the next tick can retry immediately
    // instead of waiting a full lease; rethrowing is what makes the base class
    // log it and back off, so a parser that is down does not become a hot loop.
    const id = await seedJob();
    const source = new ScriptedSource([new ParserUnavailableError('parser is down')]);

    await expect(laneFor(source).runOnce(fresh())).rejects.toThrow(/parser is down/);

    expect((await readJob(id)).status).toBe('pending');
  });
});

describe('guards against burning the request budget for nothing', () => {
  it('refuses to loop on a cursor that does not advance', async () => {
    // `has_more` set with the same cursor back means the next request is
    // byte-for-byte the one just made. Following it is an infinite loop that
    // spends the IP-based rate limit (F2) at full speed and never terminates --
    // and every page of it looks like a healthy 200.
    const id = await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(20, PAGE_1_CURSOR, true),
    ]);

    await expect(laneFor(source).runOnce(fresh())).rejects.toThrow(/cursor/i);

    expect(source.calls).toHaveLength(2);
    expect((await readJob(id)).status).toBe('pending');
  });

  it('refuses a cursor that moves BACKWARDS through the profile', async () => {
    // Phase 5 review. The first version of this guard only rejected a cursor
    // EQUAL to the current one, which leaves the more dangerous shape open: a
    // page answering `has_more` with a cursor that has gone UP walks back
    // towards the newest video, and page one comes round again. The lane then
    // oscillates forever -- lease renewed every page, `enumerated_count`
    // climbing, one request every 3-5s against the IP budget -- and `runOnce`
    // never returns, so the job queue behind it never advances either.
    //
    // Rejecting it is only possible because the direction is now KNOWN: the
    // cursor is the create_time of the oldest item in the page and profiles page
    // newest-first, so it descends. That is the same fact the checkpoint rule
    // rests on, used a second time.
    const id = await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_2_CURSOR, true),
      page(20, PAGE_1_CURSOR, true), // LATER than the page before it
    ]);

    await expect(laneFor(source).runOnce(fresh())).rejects.toThrow(/cursor/i);

    expect(source.calls).toHaveLength(2);
    expect((await readJob(id)).status).toBe('pending');
  });

  it('accepts the first page whatever cursor it returns', async () => {
    // The boundary the guard must not break. A fresh job starts at cursor 0 and
    // every real first page answers with a large millisecond timestamp, so a
    // naive "must decrease" would reject every profile in existence on its
    // first request.
    await seedJob();
    const source = new ScriptedSource([
      page(20, PAGE_1_CURSOR, true),
      page(1, PAGE_2_CURSOR, false),
    ]);

    await expect(laneFor(source).runOnce(fresh())).resolves.toBe('worked');
  });

  it('truncates a fractional cursor rather than handing it to Postgres', async () => {
    // `max_cursor` is a bigint column and the parser's number coercion accepts
    // non-integers, so a cursor of `...600000.5` reaches the parameter and
    // Postgres rejects the whole statement: `invalid input syntax for type
    // bigint`. The tick fails safely (the claim is released and the base class
    // backs off) but the job makes no progress, ever, and the log says nothing
    // about a decimal point.
    const id = await seedJob();
    const source = new ScriptedSource([page(2, PAGE_1_CURSOR + 0.5, false)]);

    await expect(laneFor(source).runOnce(fresh())).resolves.toBe('worked');

    const job = await readJob(id);
    expect(job.status).toBe('downloading');
    expect(job.maxCursor).toBe(PAGE_1_CURSOR);
  });

  it('claims one job at a time, leaving the second queued (F2)', async () => {
    // Not a semaphore: this lane is a single loop, so a second job simply is
    // not looked at until the first is finished. The system-wide guarantee is
    // asserted through the production wiring in
    // enumerate-global-concurrency.spec.ts.
    const first = await seedJob();
    const second = await seedJob();
    const source = new ScriptedSource([
      page(1, PAGE_1_CURSOR, false),
      page(1, PAGE_1_CURSOR, false),
    ]);
    const lane = laneFor(source);

    await lane.runOnce(fresh());

    expect((await readJob(first)).status).toBe('downloading');
    expect((await readJob(second)).status).toBe('pending');
  });
});

describe('items Douyin sent that cannot become rows', () => {
  it('drops the unusable ones, keeps the rest, and still advances the cursor', async () => {
    // A page whose items are partly malformed must not take the tick down: the
    // cursor still has to advance or the lane re-fetches the same broken page
    // forever. `enumerated_count` counts what Douyin SENT, which is what makes
    // it a valid progress yardstick for the cursor gate in `checkpoint`.
    const id = await seedJob();
    const good = aweme();
    const source = new ScriptedSource([
      {
        awemeList: [good, { aweme_id: '', create_time: 1 }, { desc: 'no id at all' }],
        hasMore: false,
        maxCursor: PAGE_1_CURSOR,
        statusCode: 0,
      },
    ]);

    await laneFor(source).runOnce(fresh());

    expect((await allVideos()).map((v) => v.awemeId)).toEqual([good.aweme_id]);
    const job = await readJob(id);
    expect(job.status).toBe('downloading');
    expect(job.totalVideos).toBe(1);
    expect(job.enumeratedCount).toBe(3);
    // Named exactly. `toMatch(/2/)` was the first version and it was satisfied
    // by any `2` anywhere in the accumulated log -- in practice by the job's own
    // UUID, about seven runs in eight. A flaky green is worse than a hard one:
    // it fails rarely enough to be dismissed as noise.
    expect(logged.join(' ')).toMatch(/2 item\(s\) dropped/);
  });
});

describe('what the lane says out loud', () => {
  it('never writes the sec_user_id into a log line', async () => {
    // Renamed and re-pointed. The first version asserted only
    // `not.toMatch(/sessionid|passport_csrf_token/i)`, which no mutation of this
    // lane can ever violate: the cookie lives in the parser fork and is read
    // from Redis, so it is never in scope here. That is an assertion that cannot
    // fail dressed as a security test -- and meanwhile the `sec_user_id` half of
    // its own title was not asserted at all.
    //
    // `sec_user_id` is the crawl-identifying value in this system: it is what a
    // log line would have to leak for someone reading logs to learn which
    // profiles are being archived. It is also the one secret-shaped thing the
    // lane genuinely does hold, which is what makes this falsifiable -- proven
    // during review by appending the profile URL to the success line.
    await seedJob();
    await laneFor(new ScriptedSource([page(2, PAGE_1_CURSOR, false)])).runOnce(fresh());

    expect(logged.join('\n')).not.toContain(PROFILE);
  });

  it('says which job it is working on, so a stuck enumerate is identifiable', async () => {
    const id = await seedJob();
    await laneFor(new ScriptedSource([page(2, PAGE_1_CURSOR, false)])).runOnce(fresh());

    expect(logged.join('\n')).toContain(id);
  });
});
