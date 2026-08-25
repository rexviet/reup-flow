/**
 * Phase 6 -- the download lane's ordinary life (§6.2, §6.3).
 *
 * Real Postgres, real HTTP, real filesystem. The only fake is the parser's
 * `fetchOneVideo`, and it is fake precisely so this suite can COUNT it: F1's
 * whole claim is that a warm cache costs zero calls to it, and "zero" is only
 * measurable against something that counts.
 *
 * The two races -- reaper hands the video over mid-download, operator cancels
 * mid-download -- live in `download-commit-race.spec.ts`, because both need two
 * writers genuinely overlapping and that needs a harness of its own.
 */
import { eq } from 'drizzle-orm';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Pool } from 'pg';
import { UndiciCdnClient } from '../../src/cdn/cdn.client';
import { jobs, profiles, videos, type Job, type Video } from '../../src/db/schema';
import { DownloadLane, DOWNLOAD_IDLE_MS } from '../../src/lanes/download.lane';
import type { LaneLogger } from '../../src/lanes/lane.runner';
import type { VideoPlayAddr } from '../../src/parser/parser.client';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import { videoKey } from '../../src/storage/storage.interface';
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

jest.setTimeout(90_000);

const SCHEMA = testSchemaName('download_lane');
const PROFILE = 'MS4wLjABAAAA_dl_lane';
const LEASE_SECONDS = 300;
const MAX_ATTEMPTS = 3;
const URL_TTL_SECONDS = 7200;

let pool: Pool;
let db: ReturnType<typeof testDb>;
let root: string;
let storage: LocalDiskStorage;
let cdnClient: UndiciCdnClient;
const cdn = new FakeCdn();

/** Everything the lane said, so a test can assert what is NOT in there. */
const lines: string[] = [];
const logger: LaneLogger = {
  error: (m) => lines.push(m),
  warn: (m) => lines.push(m),
  log: (m) => lines.push(m),
};

/**
 * `fetchOneVideo`, counted.
 *
 * F1's DoD line is "0 lần gọi `fetchOneVideo`", which is a claim about a
 * number. `calls` is that number; `nextUrl` is what the re-resolve hands back.
 */
class CountingResolver {
  readonly calls: string[] = [];
  nextUrl: string | null = null;
  failWith: Error | null = null;

  async fetchOneVideo(awemeId: string): Promise<VideoPlayAddr> {
    this.calls.push(awemeId);
    if (this.failWith) throw this.failWith;
    return {
      playAddrUrls: this.nextUrl === null ? [] : [this.nextUrl],
      statusCode: 0,
    };
  }
}

let resolver: CountingResolver;

function makeLane(overrides: Partial<ConstructorParameters<typeof DownloadLane>[5]> = {}) {
  return new DownloadLane(logger, db, resolver, cdnClient, storage, {
    index: 1,
    leaseSeconds: LEASE_SECONDS,
    maxAttempts: MAX_ATTEMPTS,
    urlTtlSeconds: URL_TTL_SECONDS,
    ...overrides,
  });
}

async function seedJob(status = 'downloading'): Promise<string> {
  await db.insert(profiles).values({ secUserId: PROFILE }).onConflictDoNothing();
  const [row] = await db
    .insert(jobs)
    .values({ secUserId: PROFILE, status })
    .returning({ id: jobs.id });
  return row!.id;
}

interface SeedVideo {
  awemeId: string;
  playAddr?: string | null;
  /** Seconds from now; negative means already expired. */
  urlTtl?: number | null;
  secUserId?: string;
}

async function seedVideo(v: SeedVideo): Promise<void> {
  await db.insert(videos).values({
    awemeId: v.awemeId,
    secUserId: v.secUserId ?? PROFILE,
    createTime: new Date(Date.UTC(2026, 0, 1)),
    playAddr: v.playAddr ?? null,
  });
  if (v.urlTtl !== undefined && v.urlTtl !== null) {
    await pool.query(
      `UPDATE videos SET url_expires_at = now() + make_interval(secs => $2) WHERE aweme_id = $1`,
      [v.awemeId, v.urlTtl],
    );
  }
}

async function readVideo(awemeId: string): Promise<Video> {
  const [row] = await db.select().from(videos).where(eq(videos.awemeId, awemeId));
  if (!row) throw new Error(`video ${awemeId} not found`);
  return row;
}

async function readJob(id: string): Promise<Job> {
  const [row] = await db.select().from(jobs).where(eq(jobs.id, id));
  if (!row) throw new Error(`job ${id} not found`);
  return row;
}

async function filesUnder(secUserId = PROFILE): Promise<string[]> {
  try {
    return (await readdir(join(root, 'douyin', secUserId))).sort();
  } catch {
    return [];
  }
}

const never = new AbortController().signal;

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  pool = createTestPool(SCHEMA);
  await applyForwardMigration(pool);
  db = testDb(pool);
  await cdn.start();
});

afterAll(async () => {
  await cdn.stop();
  await pool.end();
  await dropSchema(SCHEMA);
});

beforeEach(async () => {
  root = await mkdtemp(join(tmpdir(), 'reup-download-lane-'));
  storage = new LocalDiskStorage(root);
  cdnClient = new UndiciCdnClient({ timeoutMs: 5_000 });
  resolver = new CountingResolver();
  cdn.reset();
  lines.length = 0;
});

afterEach(async () => {
  await cdnClient.close();
  await truncateAll(pool);
  await rm(root, { recursive: true, force: true });
});

describe('an empty queue', () => {
  it('reports idle, and idles for a second', async () => {
    // The queue is Postgres and the claim is a partial-index lookup, so a
    // one-second poll costs approximately nothing. This is NOT a Douyin
    // request rate: the CDN is not risk-controlled (§6.1), which is the whole
    // reason download concurrency is 6 and enumerate's is 1.
    const lane = makeLane();

    expect(await lane.runOnce(never)).toBe('idle');
    expect(lane.idleDelayMs).toBe(DOWNLOAD_IDLE_MS);
  });
});

describe('the ordinary path -- a cached URL that still works', () => {
  it('downloads the video, commits it, and records where it went', async () => {
    const jobId = await seedJob();
    const payload = Buffer.alloc(120_000, 0x21);
    cdn.serveBytes(payload);
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    expect(await makeLane().runOnce(never)).toBe('worked');

    const row = await readVideo('a1');
    expect(row.status).toBe('done');
    expect(row.storagePath).toBe(join(root, videoKey(PROFILE, 'a1')));
    expect(await readFile(row.storagePath!)).toEqual(payload);
    expect(row.leaseExpiresAt).toBeNull();
    // One video, one job, nothing else outstanding -- step 8.
    expect((await readJob(jobId)).status).toBe('completed');
  });

  it('spends ZERO calls on fetchOneVideo -- F1, the whole point', async () => {
    // The DoD line, and the arithmetic behind it: re-resolving every video
    // costs ~525 requests for a 500-video profile against ~25, which is 3-5x
    // the hourly budget and a near-certain ban. IDEA.md §3.2 asks for the
    // expensive version on purpose; plan §7 records this as a deliberate
    // deviation. If this assertion ever has to be relaxed, read §7 first.
    await seedJob();
    cdn.serveBytes(Buffer.alloc(4096, 1));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });
    await seedVideo({ awemeId: 'a2', playAddr: cdn.url('/a2.mp4'), urlTtl: 3600 });

    const lane = makeLane();
    await lane.runOnce(never);
    await lane.runOnce(never);

    expect(resolver.calls).toEqual([]);
    expect(cdn.downloads).toHaveLength(2);
  });

  it('never writes the signed URL into a log line', async () => {
    // `play_addr` is a signed URL: its query string IS a credential. The
    // redaction layer knows about cookies and the API key, not about this, so
    // the only thing keeping it out of the logs is that nothing logs it.
    await seedJob();
    cdn.serveBytes(Buffer.alloc(256, 1));
    const url = cdn.url('/a1.mp4', 'super-secret-signature');
    await seedVideo({ awemeId: 'a1', playAddr: url, urlTtl: 3600 });

    await makeLane().runOnce(never);

    expect(lines.join('\n')).not.toContain('super-secret-signature');
    expect(lines.join('\n')).not.toContain(url);
    // And it did log something about the video, so the assertion above is not
    // vacuously true of a lane that logs nothing at all.
    expect(lines.join('\n')).toContain('a1');
  });
});

describe('F1 -- the reactive re-resolve', () => {
  it('re-resolves when url_expires_at has already passed', async () => {
    await seedJob();
    const payload = Buffer.alloc(2048, 2);
    cdn.serveBytes(payload);
    // A stale row: the URL is there, but the TTL says it cannot be trusted.
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/old.mp4'), urlTtl: -60 });
    resolver.nextUrl = cdn.url('/fresh.mp4');

    expect(await makeLane().runOnce(never)).toBe('worked');

    expect(resolver.calls).toEqual(['a1']);
    // It used the NEW address, not the expired one it was holding.
    expect(cdn.downloads.map((d) => d.path)).toEqual(['/fresh.mp4']);
    expect((await readVideo('a1')).status).toBe('done');
  });

  it('caches the freshly resolved URL, so the next attempt is free', async () => {
    await seedJob();
    cdn.serveBytes(Buffer.alloc(512, 3));
    await seedVideo({ awemeId: 'a1', playAddr: null });
    resolver.nextUrl = cdn.url('/fresh.mp4');

    await makeLane().runOnce(never);

    const row = await readVideo('a1');
    expect(row.playAddr).toBe(cdn.url('/fresh.mp4'));
    expect(row.urlExpiresAt!.getTime()).toBeGreaterThan(Date.now() + 3_000_000);
  });

  it('re-resolves when the enumerate response carried no play_addr at all', async () => {
    await seedJob();
    cdn.serveBytes(Buffer.alloc(512, 3));
    await seedVideo({ awemeId: 'a1', playAddr: null, urlTtl: 3600 });
    resolver.nextUrl = cdn.url('/fresh.mp4');

    expect(await makeLane().runOnce(never)).toBe('worked');
    expect(resolver.calls).toEqual(['a1']);
  });

  it.each([403, 410])(
    'when a FRESH url answers %i, re-resolves once and then succeeds',
    async (status) => {
      // The DoD line. A signed URL can die before its TTL says it should --
      // Douyin's expiry is its own business, and the cache is only ever a
      // guess. Reactive re-resolve is what makes the guess safe: F1 does not
      // trust the cached URL, it just declines to CHECK it in advance.
      await seedJob();
      const payload = Buffer.alloc(9_000, 4);
      cdn.serveOnlyForToken('good', payload, status);
      await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4', 'stale'), urlTtl: 3600 });
      resolver.nextUrl = cdn.url('/a1.mp4', 'good');
      // The fixture is checked against the status under test before anything
      // else runs. Without this, a fixture that always answered 403 would make
      // the 410 case a duplicate of the 403 case while reporting two passes --
      // exactly the "easier world" false green plan §5 rule 7 is about.
      const probe = await cdnClient.get(cdn.url('/a1.mp4', 'stale'), never);
      expect(probe.status).toBe(status);
      await probe.discard();
      cdn.reset();

      expect(await makeLane().runOnce(never)).toBe('worked');

      expect(resolver.calls).toEqual(['a1']);
      // Two GETs: the refused one, then the re-resolved one.
      expect(cdn.downloads).toHaveLength(2);
      expect(cdn.downloads[0]!.query.get('token')).toBe('stale');
      expect(cdn.downloads[1]!.query.get('token')).toBe('good');
      expect(await readFile((await readVideo('a1')).storagePath!)).toEqual(payload);
    },
  );

  it('re-resolves at most ONCE per attempt, even if the new URL is refused too', async () => {
    // Otherwise a CDN answering 403 to everything becomes an unbounded
    // `fetch_one_video` loop against the rate-limited API -- the exact failure
    // F1 exists to avoid, reached from the opposite direction.
    await seedJob();
    cdn.serveOnlyForToken('unobtainable', Buffer.alloc(16, 1));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4', 'stale'), urlTtl: 3600 });
    resolver.nextUrl = cdn.url('/a1.mp4', 'also-stale');

    await expect(makeLane().runOnce(never)).rejects.toThrow(/403/);

    expect(resolver.calls).toEqual(['a1']);
    expect(cdn.downloads).toHaveLength(2);
    expect(await filesUnder()).toEqual([]);
  });

  it('fails the attempt when Douyin has no play_addr to give', async () => {
    await seedJob();
    await seedVideo({ awemeId: 'a1', playAddr: null });
    resolver.nextUrl = null;

    await expect(makeLane().runOnce(never)).rejects.toThrow(/no play_addr/);

    const row = await readVideo('a1');
    expect(row.status).toBe('pending');
    expect(row.attempts).toBe(1);
    expect(row.lastError).toMatch(/no play_addr/);
  });
});

describe('a body with no bytes in it', () => {
  it('refuses a response that advertises zero bytes', async () => {
    // A video is never 0 bytes, and the truncation check cannot catch this one:
    // `0 !== 0` is false, so an empty body matching an empty Content-Length
    // sails through verification, gets fsync'd, renamed, and marked `done`.
    // `claimOne` never looks at a `done` row again, so there is no detector and
    // no way back -- the archive just holds an unplayable file, and the M1 DoD
    // line ("one file per downloaded_count") still passes.
    await seedJob();
    cdn.respondWith((_req, res) => {
      res.writeHead(200, { 'content-type': 'video/mp4', 'content-length': '0' });
      res.end();
    });
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    await expect(makeLane().runOnce(never)).rejects.toThrow(/no bytes|zero/i);

    const row = await readVideo('a1');
    expect(row.status).toBe('pending');
    expect(row.storagePath).toBeNull();
    expect(await filesUnder()).toEqual([]);
  });

  it('refuses an empty body that advertised no length at all', async () => {
    // The same hole reached from the other side: with no Content-Length there is
    // nothing to compare against, so the byte count cannot help here either.
    await seedJob();
    cdn.respondWith((_req, res) => {
      res.writeHead(200, { 'content-type': 'video/mp4', 'transfer-encoding': 'chunked' });
      res.end();
    });
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    await expect(makeLane().runOnce(never)).rejects.toThrow(/no bytes|zero/i);

    expect((await readVideo('a1')).status).toBe('pending');
    expect(await filesUnder()).toEqual([]);
  });

  it('still accepts a chunked body with no advertised length, and says it was unverified', async () => {
    // The positive control for the two above -- an instrument that rejected
    // every unverifiable body would report perfect protection -- and the honest
    // record of what a missing Content-Length costs: the transfer is committed
    // with no truncation check at all, so the operator gets told.
    await seedJob();
    const payload = Buffer.alloc(6_000, 0x33);
    cdn.respondWith((_req, res) => {
      res.writeHead(200, { 'content-type': 'video/mp4', 'transfer-encoding': 'chunked' });
      res.end(payload);
    });
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    expect(await makeLane().runOnce(never)).toBe('worked');

    expect(await readFile((await readVideo('a1')).storagePath!)).toEqual(payload);
    expect(lines.join('\n')).toMatch(/advertised no Content-Length/);
  });
});

describe('a cancelled job', () => {
  it('skips the video without touching the CDN at all', async () => {
    await seedJob('cancelled');
    cdn.serveBytes(Buffer.alloc(64, 1));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    expect(await makeLane().runOnce(never)).toBe('worked');

    const row = await readVideo('a1');
    expect(row.status).toBe('skipped');
    // `skipped`, not `failed`: nothing went wrong, so nothing should read as
    // wrong. And no error text, for the same reason.
    expect(row.lastError).toBeNull();
    expect(row.leaseExpiresAt).toBeNull();
    expect(cdn.downloads).toEqual([]);
    expect(await filesUnder()).toEqual([]);
  });

  it('still downloads when a cancelled job sits beside a live one', async () => {
    // One cancel must not poison the profile forever. `videos` has no `job_id`
    // (F7), so this is the only place the distinction can be made.
    await seedJob('cancelled');
    await seedJob('downloading');
    cdn.serveBytes(Buffer.alloc(64, 5));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    await makeLane().runOnce(never);

    expect((await readVideo('a1')).status).toBe('done');
  });
});

describe('retry and giving up (§6.3)', () => {
  it('hands a failed video back to pending and records why', async () => {
    await seedJob();
    cdn.respondWith((_req, res) => res.writeHead(500).end('boom'));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    await expect(makeLane().runOnce(never)).rejects.toThrow();

    const row = await readVideo('a1');
    expect(row.status).toBe('pending');
    expect(row.attempts).toBe(1);
    expect(row.lastError).toBeTruthy();
    expect(await filesUnder()).toEqual([]);
  });

  it('gives up after MAX_ATTEMPTS and does NOT kill the job', async () => {
    // §6.3: "đi tiếp, không kill cả job". A single unplayable video must not
    // take a 500-video profile down with it.
    const jobId = await seedJob();
    cdn.respondWith((_req, res) => res.writeHead(500).end('boom'));
    await seedVideo({ awemeId: 'bad', playAddr: cdn.url('/bad.mp4'), urlTtl: 3600 });

    const lane = makeLane();
    for (let i = 0; i < MAX_ATTEMPTS; i += 1) {
      await expect(lane.runOnce(never)).rejects.toThrow();
    }

    const row = await readVideo('bad');
    expect(row.attempts).toBe(MAX_ATTEMPTS);
    expect(row.status).toBe('failed');
    // The claim query filters on `attempts < MAX_ATTEMPTS`, so it is really out
    // of the queue now.
    expect(await lane.runOnce(never)).toBe('idle');
    // And the job finished, counting the failure honestly.
    const job = await readJob(jobId);
    expect(job.status).toBe('completed');
    expect(job.failedCount).toBe(1);
    expect(job.downloadedCount).toBe(0);
  });

  it('carries on with the next video after one fails', async () => {
    // `maxAttempts: 1` so the bad video gives up on its first try and leaves
    // the queue -- otherwise it is still the oldest `pending` row and the next
    // tick claims it again, which is correct behaviour but not what this test
    // is about.
    const jobId = await seedJob();
    const payload = Buffer.alloc(3000, 6);
    cdn.respondWith((req, res) => {
      if (req.url?.startsWith('/bad')) return void res.writeHead(500).end('boom');
      res.writeHead(200, { 'content-length': String(payload.length) }).end(payload);
    });
    // `bad` is older, so it is claimed first (claimOne orders by create_time).
    await db.insert(videos).values([
      {
        awemeId: 'bad',
        secUserId: PROFILE,
        createTime: new Date(Date.UTC(2026, 0, 1)),
        playAddr: cdn.url('/bad.mp4'),
      },
      {
        awemeId: 'good',
        secUserId: PROFILE,
        createTime: new Date(Date.UTC(2026, 0, 2)),
        playAddr: cdn.url('/good.mp4'),
      },
    ]);
    await pool.query("UPDATE videos SET url_expires_at = now() + interval '1 hour'");

    const lane = makeLane({ maxAttempts: 1 });
    await expect(lane.runOnce(never)).rejects.toThrow();
    expect(await lane.runOnce(never)).toBe('worked');

    expect((await readVideo('bad')).status).toBe('failed');
    expect((await readVideo('good')).status).toBe('done');
    // Finished, with the failure counted honestly rather than hidden.
    const job = await readJob(jobId);
    expect(job.status).toBe('completed');
    expect(job.downloadedCount).toBe(1);
    expect(job.failedCount).toBe(1);
  });

  it('backs off exponentially between failed ticks, without stubbing the backoff away', async () => {
    // The backoff is the base class's, so what is asserted here is that a
    // failed download really does report failure UP -- a lane that swallowed
    // the error would keep re-claiming the same broken video at full speed.
    // The delays themselves are read off `onBackoff`, not replaced by zeros:
    // a seam stubbed to 0 is a seam not tested.
    await seedJob();
    cdn.respondWith((_req, res) => res.writeHead(500).end('boom'));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    const lane = makeLane();
    const waits: number[] = [];
    lane.onBackoff = (ms) => waits.push(ms);
    lane.errorBackoffMs = 1;
    lane.start();
    const deadline = Date.now() + 10_000;
    while (waits.length < 3 && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 20));
    }
    await lane.stop();

    expect(waits.slice(0, 3)).toEqual([1, 2, 4]);
  });
});

describe('step 8 -- finalizing the job', () => {
  it('leaves the job alone while videos are still outstanding', async () => {
    const jobId = await seedJob();
    cdn.serveBytes(Buffer.alloc(128, 7));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });
    await seedVideo({ awemeId: 'a2', playAddr: cdn.url('/a2.mp4'), urlTtl: 3600 });

    await makeLane().runOnce(never);

    expect((await readJob(jobId)).status).toBe('downloading');
  });

  it('completes the job on the last video, with counters from profile state', async () => {
    const jobId = await seedJob();
    cdn.serveBytes(Buffer.alloc(128, 7));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });
    await seedVideo({ awemeId: 'a2', playAddr: cdn.url('/a2.mp4'), urlTtl: 3600 });

    const lane = makeLane();
    await lane.runOnce(never);
    await lane.runOnce(never);

    const job = await readJob(jobId);
    expect(job.status).toBe('completed');
    expect(job.downloadedCount).toBe(2);
    expect(job.totalVideos).toBe(2);
  });

  it('never finalizes another profile\'s job', async () => {
    const mine = await seedJob();
    await db.insert(profiles).values({ secUserId: 'someone-else' }).onConflictDoNothing();
    const [other] = await db
      .insert(jobs)
      .values({ secUserId: 'someone-else', status: 'downloading' })
      .returning({ id: jobs.id });
    cdn.serveBytes(Buffer.alloc(64, 8));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    await makeLane().runOnce(never);

    expect((await readJob(mine)).status).toBe('completed');
    // The other profile has NO videos, so "nothing outstanding" is vacuously
    // true of it -- and an unscoped finalize would complete a job that has not
    // enumerated a single item.
    expect((await readJob(other!.id)).status).toBe('downloading');
  });
});

describe('shutdown', () => {
  it('claims nothing once the signal is already aborted', async () => {
    await seedJob();
    cdn.serveBytes(Buffer.alloc(64, 9));
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });
    const aborter = new AbortController();
    aborter.abort();

    expect(await makeLane().runOnce(aborter.signal)).toBe('idle');

    expect((await readVideo('a1')).status).toBe('pending');
    expect((await readVideo('a1')).attempts).toBe(0);
    expect(cdn.downloads).toEqual([]);
  });

  it('hands the claim back when a download is interrupted by SIGTERM', async () => {
    // §4.2's "nhả lease", for downloads. Without it the restarted process sits
    // beside this video for a full LEASE_SECONDS unable to claim it.
    await seedJob();
    const gate = new Promise(() => {});
    cdn.serveGated(Buffer.alloc(200_000, 1), gate);
    await seedVideo({ awemeId: 'a1', playAddr: cdn.url('/a1.mp4'), urlTtl: 3600 });

    const lane = makeLane();
    lane.start();
    // Wait until the download is genuinely under way.
    const deadline = Date.now() + 10_000;
    while (cdn.downloads.length === 0 && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 10));
    }
    await lane.stop();

    const row = await readVideo('a1');
    expect(row.status).toBe('pending');
    expect(row.leaseExpiresAt).toBeNull();
    // The attempt is still spent -- that is F6's accepted tradeoff, not a bug.
    expect(row.attempts).toBe(1);
    expect(await filesUnder()).toEqual([]);
  });
});
