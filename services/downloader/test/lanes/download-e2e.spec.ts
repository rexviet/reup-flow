/**
 * Phase 6 -- the M1 GATE.
 *
 * Two DoD lines that no unit test can stand in for:
 *
 *   "Tải xong 1 profile <50 video từ đầu đến cuối, số file trên đĩa khớp
 *    `downloaded_count`" -- with the production number of lanes running
 *    concurrently against one queue, because "six loops never exceed six
 *    downloads" and "six loops never claim the same video twice" are properties
 *    of the whole arrangement, not of one lane.
 *
 *   "Kill process giữa download → restart → không tải lại file đã done" -- with
 *    a real `SIGKILL` at a real process, because every in-process "crash" runs
 *    cleanup code of ours and the question is what survives with NO cleanup.
 *
 * Plus the one that is easy to state and easy to get wrong: "Không có `.tmp`
 * sót lại sau khi mọi job xong."
 */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { mkdtemp, readFile, readdir, rm, stat, utimes } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Pool } from 'pg';
import { UndiciCdnClient } from '../../src/cdn/cdn.client';
import * as videosRepository from '../../src/db/videos.repository';
import { DownloadLane } from '../../src/lanes/download.lane';
import type { LaneLogger, LaneRunner } from '../../src/lanes/lane.runner';
import { ReaperLane } from '../../src/lanes/reaper.lane';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import { videoKey } from '../../src/storage/storage.interface';
import {
  TEST_DATABASE_URL,
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testSchemaName,
} from '../support/db';
import { channelCollection } from '../support/collections';
import { FakeCdn } from '../support/fake-cdn';
import { waitUntil } from '../support/streams';

jest.setTimeout(180_000);

const SCHEMA = testSchemaName('download_e2e');
const PROFILE = 'MS4wLjABAAAA_dl_e2e';
/** Under 50, as the DoD says, and enough that six lanes really do overlap. */
const VIDEO_COUNT = 40;
const LEASE_SECONDS = 300;
const MAX_ATTEMPTS = 3;
/** The production default (env.ts). This test runs the real number of loops. */
const DOWNLOAD_CONCURRENCY = 6;

let pool: Pool;
let root: string;
const cdn = new FakeCdn();

const lines: string[] = [];
const logger: LaneLogger = {
  error: (m) => lines.push(m),
  warn: (m) => lines.push(m),
  log: (m) => lines.push(m),
};

/** Deterministic, and a DIFFERENT LENGTH per video, so a mixed-up file shows. */
function payloadFor(i: number): Buffer {
  return Buffer.alloc(4_000 + i * 137, (i % 251) + 1);
}

function awemeIdFor(i: number): string {
  return `e2e-${String(i).padStart(3, '0')}`;
}

/** Serves `/{aweme_id}.mp4` from `payloadFor`, refusing anything it does not know. */
function serveAllVideos(): void {
  cdn.respondWith((req, res) => {
    const match = /^\/e2e-(\d+)\.mp4/.exec(req.url ?? '');
    if (!match) return void res.writeHead(404).end();
    const payload = payloadFor(Number(match[1]));
    res.writeHead(200, { 'content-length': String(payload.length) });
    res.end(payload);
  });
}

function schemaScopedDsn(): string {
  const url = new URL(TEST_DATABASE_URL);
  url.searchParams.set('options', `-c search_path=${SCHEMA}`);
  return url.toString();
}

async function seedProfileAndJob(): Promise<string> {
  await pool.query('INSERT INTO profiles (sec_user_id) VALUES ($1)', [PROFILE]);
  const { rows } = await pool.query(
    "INSERT INTO jobs (sec_user_id, status, total_videos, enumerated_count) VALUES ($1, 'downloading', $2, $2) RETURNING id",
    [PROFILE, VIDEO_COUNT],
  );
  return (rows[0] as { id: string }).id;
}

async function seedVideos(count: number): Promise<void> {
  const collectionId = await channelCollection(pool, PROFILE);
  for (let i = 0; i < count; i += 1) {
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, play_addr, url_expires_at, collection_id)
       VALUES ($1, $2, now() - make_interval(secs => $3), $4, now() + interval '1 hour', $5)`,
      [awemeIdFor(i), PROFILE, count - i, cdn.url(`/${awemeIdFor(i)}.mp4`), collectionId],
    );
  }
}

/** Every file under DATA_DIR, recursively, relative to the root. */
async function allFiles(dir = root, prefix = ''): Promise<string[]> {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return [];
  }
  const out: string[] = [];
  for (const entry of entries) {
    const rel = prefix === '' ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) out.push(...(await allFiles(join(dir, entry.name), rel)));
    else out.push(rel);
  }
  return out.sort();
}

async function countByStatus(): Promise<Record<string, number>> {
  const { rows } = await pool.query(
    'SELECT status, count(*)::int AS n FROM videos GROUP BY status',
  );
  return Object.fromEntries((rows as { status: string; n: number }[]).map((r) => [r.status, r.n]));
}

/**
 * N lanes over ONE pool and ONE storage instance, as `lanes.module.ts` builds
 * them. Both halves matter: `LocalDiskStorage` tracks the temp files it is
 * writing, and a second instance would know none of the first's.
 */
function makeLanes(cdnClient: UndiciCdnClient, storage: LocalDiskStorage, n: number): LaneRunner[] {
  return Array.from({ length: n }, (_unused, i) => makeLane(cdnClient, storage, i + 1));
}

let laneDb: ReturnType<typeof import('../support/db').testDb>;

function makeLane(
  cdnClient: UndiciCdnClient,
  storage: LocalDiskStorage,
  index: number,
): DownloadLane {
  return new DownloadLane(
    logger,
    laneDb,
    {
      async fetchOneVideo() {
        throw new Error('the e2e fixture has fresh URLs; re-resolving would be a bug');
      },
    },
    cdnClient,
    storage,
    {
      index,
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
      urlTtlSeconds: 7200,
      heartbeatMs: 1_000,
    },
  );
}

beforeAll(async () => {
  await createEmptySchema(SCHEMA);
  // Generous: six lanes plus the test's own queries.
  pool = createTestPool(SCHEMA, { max: 10 });
  await applyForwardMigration(pool);
  laneDb = (await import('../support/db')).testDb(pool);
  await cdn.start();
});

afterAll(async () => {
  await cdn.stop();
  await pool.end();
  await dropSchema(SCHEMA);
});

beforeEach(async () => {
  root = await mkdtemp(join(tmpdir(), 'reup-download-e2e-'));
  cdn.reset();
  lines.length = 0;
  await pool.query('TRUNCATE videos, jobs, profiles RESTART IDENTITY CASCADE');
});

afterEach(async () => {
  await rm(root, { recursive: true, force: true });
});

describe('M1 gate: a whole profile, six lanes, start to finish', () => {
  it('downloads every video exactly once and finishes the job', async () => {
    serveAllVideos();
    const jobId = await seedProfileAndJob();
    await seedVideos(VIDEO_COUNT);

    const storage = new LocalDiskStorage(root);
    const cdnClient = new UndiciCdnClient({ timeoutMs: 10_000 });
    const lanes = makeLanes(cdnClient, storage, DOWNLOAD_CONCURRENCY);
    for (const lane of lanes) lane.start();

    try {
      await waitUntil(async () => {
        const { rows } = await pool.query('SELECT status FROM jobs WHERE id = $1', [jobId]);
        return (rows[0] as { status: string }).status === 'completed';
      }, 'the job to complete', 120_000);
    } finally {
      await Promise.all(lanes.map((lane) => lane.stop()));
      await cdnClient.close();
    }

    // Said out loud: a lane that died on its first tick would leave the job
    // unfinished, but if it died AFTER the last video the wait above would pass
    // and nothing else here would notice.
    for (const lane of lanes) expect(lane.crashedWith).toBeNull();

    const { rows } = await pool.query(
      'SELECT downloaded_count, failed_count, total_videos FROM jobs WHERE id = $1',
      [jobId],
    );
    const job = rows[0] as { downloaded_count: number; failed_count: number; total_videos: number };

    // THE DoD LINE: the number of files on disk equals `downloaded_count`.
    const files = await allFiles();
    expect(job.downloaded_count).toBe(VIDEO_COUNT);
    expect(job.failed_count).toBe(0);
    expect(job.total_videos).toBe(VIDEO_COUNT);
    expect(files).toHaveLength(job.downloaded_count);
    expect(await countByStatus()).toEqual({ done: VIDEO_COUNT });

    // EXACTLY ONE GET PER VIDEO. Six lanes on one queue must not claim a video
    // twice: the claim is `FOR UPDATE SKIP LOCKED` (phase 1) and this is what
    // that buys. A duplicate here would also mean paid-for bytes thrown away.
    expect(cdn.downloads).toHaveLength(VIDEO_COUNT);
    expect(new Set(cdn.downloads.map((d) => d.path)).size).toBe(VIDEO_COUNT);

    // And every file is the RIGHT video, byte for byte -- not merely present
    // and plausibly sized, which is what a mixed-up commit would look like.
    for (let i = 0; i < VIDEO_COUNT; i += 1) {
      const path = join(root, videoKey(PROFILE, awemeIdFor(i)));
      expect(await readFile(path)).toEqual(payloadFor(i));
    }
  });

  it('leaves no .tmp behind once every job is done', async () => {
    // The DoD line, and the risk it names: a temp file is invisible to
    // `exists()` and eats exactly the space `freeBytes()` promises the phase-10
    // preflight. One per crash-loop iteration, at full video size.
    serveAllVideos();
    const jobId = await seedProfileAndJob();
    await seedVideos(12);
    await pool.query('UPDATE jobs SET total_videos = 12 WHERE id = $1', [jobId]);

    const storage = new LocalDiskStorage(root);
    const cdnClient = new UndiciCdnClient({ timeoutMs: 10_000 });
    const lanes = makeLanes(cdnClient, storage, DOWNLOAD_CONCURRENCY);
    for (const lane of lanes) lane.start();
    try {
      await waitUntil(async () => {
        const { rows } = await pool.query('SELECT status FROM jobs WHERE id = $1', [jobId]);
        return (rows[0] as { status: string }).status === 'completed';
      }, 'the job to complete', 60_000);
    } finally {
      await Promise.all(lanes.map((lane) => lane.stop()));
      await cdnClient.close();
    }

    const files = await allFiles();
    expect(files.filter((name) => name.endsWith('.tmp'))).toEqual([]);
    expect(files).toHaveLength(12);
  });
});

/**
 * Waits for a line on the child's stdout, or rejects.
 *
 * Same helper shape as `sigterm-shutdown.e2e-spec.ts`: a child that fails to
 * start must fail the test loudly rather than time out silently.
 */
function waitForLine(proc: ChildProcessWithoutNullStreams, needle: string): Promise<void> {
  return new Promise((resolve, reject) => {
    let seen = '';
    const timer = setTimeout(() => {
      reject(new Error(`child never printed ${needle}. stdout so far:\n${seen}`));
    }, 30_000);
    proc.stdout.on('data', (chunk: Buffer) => {
      seen += chunk.toString();
      if (seen.includes(needle)) {
        clearTimeout(timer);
        resolve();
      }
    });
    proc.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`child exited with ${code} before printing ${needle}`));
    });
  });
}

describe('SIGKILL mid-download, then restart', () => {
  it('never re-downloads a video that is already done', async () => {
    // The DoD line. Nothing needs to remember what was finished: `claimOne`
    // only ever looks at `pending`, so `done` rows are invisible to it. What
    // this test proves is that the property survives a process that got no
    // chance to write anything down.
    const stalled = new Promise(() => {});
    cdn.respondWith((req, res) => {
      const match = /^\/e2e-(\d+)\.mp4/.exec(req.url ?? '');
      if (!match) return void res.writeHead(404).end();
      const i = Number(match[1]);
      const payload = payloadFor(i);
      res.writeHead(200, { 'content-length': String(payload.length) });
      // The LAST video never finishes, so the child is guaranteed to be
      // mid-download when it is killed rather than between videos.
      if (i === 2) {
        res.write(payload.subarray(0, 512));
        void stalled.then(() => res.end());
        return;
      }
      res.end(payload);
    });

    const jobId = await seedProfileAndJob();
    await seedVideos(3);
    await pool.query('UPDATE jobs SET total_videos = 3 WHERE id = $1', [jobId]);

    const child = spawn(
      process.execPath,
      [
        '-r',
        'ts-node/register/transpile-only',
        join(__dirname, '..', 'support', 'download-crash-child.ts'),
      ],
      {
        cwd: join(__dirname, '..', '..'),
        env: {
          ...process.env,
          CHILD_DATABASE_URL: schemaScopedDsn(),
          CHILD_SCHEMA: SCHEMA,
          CHILD_DATA_DIR: root,
          CHILD_LEASE_SECONDS: String(LEASE_SECONDS),
          CHILD_MAX_ATTEMPTS: String(MAX_ATTEMPTS),
        },
      },
    );
    let childStderr = '';
    child.stderr.on('data', (c: Buffer) => {
      childStderr += c.toString();
    });

    try {
      await waitForLine(child, 'CHILD_RUNNING');
      // Two committed, and the third wedged in a body that will never end.
      await waitUntil(
        async () => (await countByStatus()).done === 2,
        `two videos to be committed by the child (stderr: ${childStderr})`,
        60_000,
      );
      await waitUntil(
        async () => (await countByStatus()).downloading === 1,
        'the third download to be under way',
        30_000,
      );
      // A real temp file exists -- the bytes of the download in flight. Waited
      // for rather than asserted on the spot: the row says `downloading` as soon
      // as it is CLAIMED, which is a moment before the first chunk reaches the
      // disk, and asserting into that gap would be a flaky test rather than a
      // wrong implementation.
      await waitUntil(
        async () => (await allFiles()).some((f) => f.endsWith('.tmp')),
        'the in-flight download to have a temp file on disk',
        30_000,
      );
      expect((await allFiles()).filter((f) => f.endsWith('.tmp'))).toHaveLength(1);
    } finally {
      // SIGKILL, not SIGTERM: no shutdown hook, no lease release, no unlink.
      child.kill('SIGKILL');
      await new Promise((resolve) => child.on('exit', resolve));
    }

    const afterKill = await countByStatus();
    expect(afterKill.done).toBe(2);
    // Stuck in `downloading` with a lease nobody is renewing -- invisible to
    // the claim query. This is precisely what F3's reaper exists for.
    expect(afterKill.downloading).toBe(1);

    // "Restart": a fresh lane in this process, plus the reaper that a restarted
    // process would also be running. The lease is moved into the past rather
    // than waited out -- the reaper compares against the database clock either
    // way, so this exercises the real predicate without taking five minutes.
    cdn.reset();
    await pool.query("UPDATE videos SET lease_expires_at = now() - interval '1 hour'");

    const storage = new LocalDiskStorage(root);
    const cdnClient = new UndiciCdnClient({ timeoutMs: 10_000 });
    // The third video now answers normally: the restart must be able to finish
    // what the killed process could not.
    serveAllVideos();

    // The killed child's temp file is a genuine orphan now: full of bytes,
    // invisible to `exists()`, and nothing in this process knows it was ever
    // being written. The reaper decides by MTIME -- a file whose writer is alive
    // keeps being touched -- so its age is moved into the past rather than
    // waited out, exactly as the row's lease was. The cutoff itself stays the
    // production one; changing it would test a sweep production never runs.
    const orphan = (await allFiles()).find((f) => f.endsWith('.tmp'));
    expect(orphan).toBeDefined();
    const aged = new Date(Date.now() - (LEASE_SECONDS + 60) * 1000);
    await utimes(join(root, orphan!), aged, aged);

    const reaper = new ReaperLane(logger, laneDb, storage, {
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
    });
    await reaper.runOnce(new AbortController().signal);

    const lane = makeLane(cdnClient, storage, 1);
    try {
      expect(await lane.runOnce(new AbortController().signal)).toBe('worked');
    } finally {
      await cdnClient.close();
    }

    // THE ASSERTION: exactly ONE request after the restart, for the one video
    // that was not finished. The two already-done videos were not asked for
    // again -- which is the difference between resuming and starting over.
    expect(cdn.downloads.map((d) => d.path)).toEqual(['/e2e-002.mp4']);
    expect((await countByStatus()).done).toBe(3);

    // And the killed process's orphaned temp file is gone -- the reaper's
    // `.tmp` sweep, on a real SIGKILLed writer rather than a simulated one.
    const files = await allFiles();
    expect(files.filter((f) => f.endsWith('.tmp'))).toEqual([]);
    expect(files).toHaveLength(3);
    for (let i = 0; i < 3; i += 1) {
      const path = join(root, videoKey(PROFILE, awemeIdFor(i)));
      expect((await stat(path)).size).toBe(payloadFor(i).length);
    }

    // The job finished, and its counters agree with the disk. Asserted flatly:
    // an earlier version read `finalized.length + (completed ? 1 : 0) > 0` AFTER
    // calling `finalizeSettled` itself, which scores 1 whether the LANE
    // finalized the job or the test's own call did -- a disjunction that cannot
    // fail in the direction it was written for.
    const { rows } = await pool.query('SELECT status, downloaded_count FROM jobs WHERE id = $1', [
      jobId,
    ]);
    const job = rows[0] as { status: string; downloaded_count: number };
    expect(job.status).toBe('completed');
    expect(job.downloaded_count).toBe(files.length);
    expect(await videosRepository.hasWorkForProfile(laneDb, PROFILE)).toBe(false);
  });
});
