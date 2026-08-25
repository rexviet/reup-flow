/**
 * phase-07 §7.2 -- THE WHOLE RECOVERY CHAIN, in one test, in order:
 *
 *   cookie ban -> `blocked`, `max_cursor` kept, videos already `done` still done
 *     -> push a fresh cookie -> the parser uses it immediately, NO RESTART
 *     -> POST /retry -> job blocked->pending, videos failed->pending, attempts=0
 *     -> the lane resumes FROM `max_cursor` and re-does nothing
 *
 * WHY THIS EXISTS BESIDE cookie-recovery.spec.ts. That file proves the
 * `blocked`-not-`failed` distinction and the checkpoint resume. It says nothing
 * about the DOWNLOAD side of the same recovery, and that is where the expensive
 * mistakes live: a recovery that re-downloads an archive is a recovery that costs
 * hours and disk, and a recovery that leaves `failed` videos with spent attempts
 * is one that reports success while quietly abandoning work. Both are invisible
 * from the enumerate side alone.
 *
 * ALSO NOT A RESTART. Every step below runs against the SAME lane instances and
 * the SAME parser object; the "new cookie" is a flag flipping on a parser that
 * was never reconstructed. That is the in-process shape of the SPIKE 2 contract
 * (NestJS writes Redis, the parser reads it on the next request) whose
 * across-the-wire half is `test/spike-cookie-swap`. Rebuilding the lane here
 * would quietly turn "no restart" into "restart", i.e. into the one thing the
 * DoD line says must not be necessary.
 *
 * The retry goes through `JobsService`, not through the repositories, because
 * "/retry" in the DoD means the production composition -- job status, failed
 * videos, and skipped videos in ONE transaction. Calling the three pieces by
 * hand would prove they exist, not that the endpoint uses them.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { Test } from '@nestjs/testing';
import { UndiciCdnClient } from '../../src/cdn/cdn.client';
import { DB } from '../../src/db/db.module';
import { jobs, profiles, videos, type Job, type Video } from '../../src/db/schema';
import { JobsService } from '../../src/jobs/jobs.service';
import { DownloadLane } from '../../src/lanes/download.lane';
import { EnumerateLane, type EnumerateParser } from '../../src/lanes/enumerate.lane';
import type { LaneLogger } from '../../src/lanes/lane.runner';
import { PARSER_CLIENT } from '../../src/parser/parser.module';
import type { ParserClient, UserPostPage, UserProfile } from '../../src/parser/parser.client';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';
import { FakeCdn } from '../support/fake-cdn';

jest.setTimeout(120_000);

const SCHEMA = testSchemaName('recovery_loop');
const PROFILE = 'MS4wLjABAAAA_recovery_loop';
const PROBE_ID = 'MS4wLjABAAAA_recovery_probe';
const LEASE_SECONDS = 300;
const MAX_ATTEMPTS = 3;

/** Descending, as Douyin's cursor really is (plan §5 rule 7). */
const PAGE_1_CURSOR = 1_735_689_600_000;
const PAGE_2_CURSOR = 1_735_603_200_000;

let pool: Pool;
let db: ReturnType<typeof testDb>;
let root: string;
let service: JobsService;
const cdn = new FakeCdn();

const lines: string[] = [];
const logger: LaneLogger = {
  error: (m) => lines.push(m),
  warn: (m) => lines.push(m),
  log: (m) => lines.push(m),
};

/**
 * One parser object for the whole scenario, whose cookie can be revoked and
 * rotated without reconstructing it.
 *
 * `revoked` answers the way a refused cookie really does: HTTP 200,
 * `aweme_list: []`, `has_more: 1`. That is §5.3's structural signal -- certain,
 * and therefore diagnosed with no probe at all, which is why `fetchUserProfile`
 * here throws rather than answering.
 */
class BannableParser implements EnumerateParser {
  revoked = false;
  readonly pageCursors: number[] = [];
  readonly videoResolves: string[] = [];

  constructor(private readonly pages: Map<number, UserPostPage>) {}

  async fetchUserPostPage(_secUserId: string, maxCursor: number): Promise<UserPostPage> {
    this.pageCursors.push(maxCursor);
    if (this.revoked) return { awemeList: [], hasMore: true, maxCursor, statusCode: 0 };
    const page = this.pages.get(maxCursor);
    if (!page) throw new Error(`no scripted page for cursor ${maxCursor}`);
    return page;
  }

  async fetchUserProfile(secUserId: string): Promise<UserProfile> {
    throw new Error(
      `the F4 probe must not run on the structural blocked signal (asked about ${secUserId})`,
    );
  }

  /** F1's re-resolve path. Counted, because a recovery must not re-resolve URLs it has. */
  async fetchOneVideo(awemeId: string) {
    this.videoResolves.push(awemeId);
    return { playAddrUrls: [cdn.url(`/${awemeId}.mp4`)], statusCode: 0 };
  }

  async close(): Promise<void> {}
}

function item(id: string, when: number): Record<string, unknown> {
  return {
    aweme_id: id,
    desc: `clip ${id}`,
    create_time: when,
    video: { duration: 9_000, play_addr: { url_list: [cdn.url(`/${id}.mp4`)] } },
  };
}

function script(): Map<number, UserPostPage> {
  return new Map<number, UserPostPage>([
    [
      0,
      {
        awemeList: [item('loop-1', 1_700_000_003), item('loop-2', 1_700_000_002)],
        hasMore: true,
        maxCursor: PAGE_1_CURSOR,
        statusCode: 0,
      },
    ],
    [
      PAGE_1_CURSOR,
      {
        awemeList: [item('loop-3', 1_700_000_001)],
        hasMore: false,
        maxCursor: PAGE_2_CURSOR,
        statusCode: 0,
      },
    ],
  ]);
}

/** Deterministic, and a DIFFERENT LENGTH per video, so a mixed-up file shows. */
function payloadFor(id: string): Buffer {
  return Buffer.alloc(2_000 + id.charCodeAt(id.length - 1), id.charCodeAt(id.length - 1));
}

/**
 * Serves any `/{aweme_id}.mp4` this suite uses, with a truthful Content-Length.
 *
 * The pattern is deliberately wide. An earlier version matched only `loop-\d`,
 * so `loop-x` -- the video the /retry reset is measured on -- answered 404 and
 * the failure looked like a bug in the retry path. A fixture that cannot serve
 * every id the test names is a fixture easier than reality in the direction that
 * hides the thing under test.
 */
function serveEveryVideo(): void {
  cdn.respondWith((req, res) => {
    const id = /^\/(loop-[0-9a-z]+)\.mp4/.exec(req.url ?? '')?.[1];
    if (!id) return void res.writeHead(404).end();
    const payload = payloadFor(id);
    res.writeHead(200, { 'content-length': String(payload.length) });
    res.end(payload);
  });
}

let parser: BannableParser;
let enumerateLane: EnumerateLane;
let downloadLane: DownloadLane;

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);
  root = await mkdtemp(join(tmpdir(), 'reup-recovery-'));
  await cdn.start();

  const moduleRef = await Test.createTestingModule({
    providers: [
      JobsService,
      { provide: DB, useValue: db },
      {
        provide: PARSER_CLIENT,
        useValue: { resolveSecUserId: async () => PROFILE } as unknown as ParserClient,
      },
    ],
  }).compile();
  service = moduleRef.get(JobsService);
});

afterAll(async () => {
  await cdn.stop();
  await pool.end();
  await dropSchema(SCHEMA);
  await rm(root, { recursive: true, force: true });
});

const cdnClient = new UndiciCdnClient();
afterAll(async () => {
  await cdnClient.close();
});

beforeEach(async () => {
  lines.length = 0;
  cdn.reset();
  await db.insert(profiles).values({ secUserId: PROFILE }).onConflictDoNothing();

  parser = new BannableParser(script());
  // ONE of each, built once per scenario and reused across every step: the
  // "no restart" half of the DoD is only observable if nothing is rebuilt.
  enumerateLane = new EnumerateLane(logger, db, parser, {
    leaseSeconds: LEASE_SECONDS,
    urlTtlSeconds: 7_200,
    sleepMsMin: 1,
    sleepMsMax: 2,
    probeSecUserId: PROBE_ID,
  });
  downloadLane = new DownloadLane(
    logger,
    db,
    parser,
    cdnClient,
    new LocalDiskStorage(root),
    {
      index: 1,
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
      urlTtlSeconds: 7_200,
      heartbeatMs: 50,
    },
  );
});

afterEach(async () => {
  await truncateAll(pool);
  for (const entry of await readdir(root)) {
    await rm(join(root, entry), { recursive: true, force: true });
  }
});

const fresh = () => new AbortController().signal;

async function readJob(id: string): Promise<Job> {
  const [row] = await db.select().from(jobs).where(eq(jobs.id, id));
  if (!row) throw new Error(`job ${id} not found`);
  return row;
}

async function readVideo(awemeId: string): Promise<Video> {
  const [row] = await db.select().from(videos).where(eq(videos.awemeId, awemeId));
  if (!row) throw new Error(`video ${awemeId} not found`);
  return row;
}

/** Every file under DATA_DIR, recursively, relative to the root. */
async function allFiles(dir = root, prefix = ''): Promise<string[]> {
  const out: string[] = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const rel = prefix === '' ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) out.push(...(await allFiles(join(dir, entry.name), rel)));
    else out.push(rel);
  }
  return out;
}

describe('§7.2 -- the full recovery loop', () => {
  it('runs end to end and re-does nothing that was already done', async () => {
    const [seeded] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    const jobId = seeded!.id;

    // ---- 1. page one lands, then the cookie is banned -----------------------
    serveEveryVideo();

    // Page one succeeds; the cookie dies before page two is asked for.
    const pages = script();
    parser = new BannableParser(pages);
    const bannedLane = new EnumerateLane(logger, db, parser, {
      leaseSeconds: LEASE_SECONDS,
      urlTtlSeconds: 7_200,
      sleepMsMin: 1,
      sleepMsMax: 2,
      probeSecUserId: PROBE_ID,
    });
    const original = parser.fetchUserPostPage.bind(parser);
    let served = 0;
    parser.fetchUserPostPage = async (secUserId: string, maxCursor: number) => {
      const page = await original(secUserId, maxCursor);
      served += 1;
      if (served === 1) parser.revoked = true;
      return page;
    };

    await bannedLane.runOnce(fresh());

    const stuck = await readJob(jobId);
    expect(stuck.status).toBe('blocked');
    expect(stuck.maxCursor).toBe(PAGE_1_CURSOR);
    expect(stuck.enumeratedCount).toBe(2);

    // ---- 2. download what page one gave us, so there IS something to keep ---
    const downloaded: string[] = [];
    for (let i = 0; i < 2; i += 1) {
      const outcome = await downloadLane.runOnce(fresh());
      if (outcome === 'worked') downloaded.push('one');
    }
    expect(downloaded).toHaveLength(2);
    expect((await readVideo('loop-1')).status).toBe('done');
    expect((await readVideo('loop-2')).status).toBe('done');
    const filesAfterFirstPass = (await allFiles()).sort();
    expect(filesAfterFirstPass).toHaveLength(2);
    const cdnHitsBeforeRecovery = cdn.downloads.length;
    expect(cdnHitsBeforeRecovery).toBe(2);

    // ---- 3. one video failed for its own reasons, attempts spent ------------
    // So the retry has something to reset, and the reset is observable.
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, status, attempts, last_error,
                           play_addr, url_expires_at)
       VALUES ('loop-x', $1, now(), 'failed', $2, 'gave up', $3, now() + interval '1 hour')`,
      [PROFILE, MAX_ATTEMPTS, cdn.url('/loop-x.mp4')],
    );

    // ---- 4. push a fresh cookie -- SAME parser object, no restart ----------
    parser.revoked = false;

    // ---- 5. /retry, through the production composition ---------------------
    const retried = await service.retry(jobId);
    expect(retried).not.toBeNull();
    expect(retried!.job.status).toBe('pending');
    expect(retried!.job.error).toBeNull();
    // The failed video came back with a clean slate; `attempts = 0` is what
    // makes it visible to `claimOne` again at all.
    expect(retried!.videosReset).toBe(1);
    const revived = await readVideo('loop-x');
    expect(revived.status).toBe('pending');
    expect(revived.attempts).toBe(0);
    expect(revived.lastError).toBeNull();
    // AND the already-done work was NOT reset. This is the assertion that makes
    // the recovery cheap rather than merely correct.
    expect((await readVideo('loop-1')).status).toBe('done');
    expect((await readVideo('loop-2')).status).toBe('done');
    // The checkpoint survived /retry too -- clearing it would be a re-page of
    // the whole profile against an IP-based rate limit.
    expect((await readJob(jobId)).maxCursor).toBe(PAGE_1_CURSOR);

    // ---- 6. the lane resumes FROM the checkpoint ---------------------------
    const cursorsBefore = parser.pageCursors.length;
    await bannedLane.runOnce(fresh());

    const resumedCursors = parser.pageCursors.slice(cursorsBefore);
    // Page one is NOT re-fetched: enumeration picks up at the checkpoint.
    expect(resumedCursors).toEqual([PAGE_1_CURSOR]);
    const finished = await readJob(jobId);
    expect(finished.status).toBe('downloading');
    expect(finished.enumeratedCount).toBe(3);

    // ---- 7. finish the profile: only what was missing is fetched ----------
    for (let i = 0; i < 4; i += 1) await downloadLane.runOnce(fresh());

    // `loop-3` (new from page two) and `loop-x` (reset) -- and NOT loop-1/2.
    const newHits = cdn.downloads.slice(cdnHitsBeforeRecovery).map((d) => d.path).sort();
    expect(newHits).toEqual(['/loop-3.mp4', '/loop-x.mp4']);
    // The DoD's "không tải lại video đã done", as a statement about the wire.
    expect(newHits).not.toContain('/loop-1.mp4');
    expect(newHits).not.toContain('/loop-2.mp4');

    // Every video settled, the job is complete, and the files on disk agree.
    const done = await readJob(jobId);
    expect(done.status).toBe('completed');
    expect(done.downloadedCount).toBe(4);
    expect((await allFiles()).sort()).toHaveLength(4);
    // No temp files left anywhere.
    expect((await allFiles()).filter((f) => f.endsWith('.tmp'))).toEqual([]);
    // F1: nothing was re-resolved. The URLs cached at enumerate time were used.
    expect(parser.videoResolves).toEqual([]);
  });

  it('a job cancelled mid-recovery still has a way back (phase 6 handover)', async () => {
    // The recovery chain and the cancel fix meet here: cancel while videos are
    // outstanding, then /retry, and the profile must be downloadable again.
    // Without the phase-7 revival this is where the loop dead-ends -- silently,
    // because `skipped` counts as settled.
    const [seeded] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    const jobId = seeded!.id;
    serveEveryVideo();

    await enumerateLane.runOnce(fresh());
    expect((await readJob(jobId)).status).toBe('downloading');

    expect(await service.cancel(jobId)).not.toBeNull();
    // Every outstanding video gets skipped by the lane -- production path.
    for (let i = 0; i < 4; i += 1) await downloadLane.runOnce(fresh());
    const statuses = await db.select().from(videos).where(eq(videos.secUserId, PROFILE));
    expect(statuses.every((v) => v.status === 'skipped')).toBe(true);
    expect(cdn.downloads).toHaveLength(0);

    const retried = await service.retry(jobId);

    expect(retried!.job.status).toBe('pending');
    expect(retried!.videosReset).toBe(3);
    for (let i = 0; i < 5; i += 1) await downloadLane.runOnce(fresh());
    expect(cdn.downloads.map((d) => d.path).sort()).toEqual([
      '/loop-1.mp4',
      '/loop-2.mp4',
      '/loop-3.mp4',
    ]);
    expect((await allFiles())).toHaveLength(3);
  });
});
