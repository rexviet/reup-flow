/**
 * Phase 6 -- the two races, and the two mutations the DoD names.
 *
 *   1. THE REAPER HANDS A VIDEO OVER WHILE ITS FIRST WORKER IS STILL STREAMING.
 *      Both workers then hold the same key. The temp name is unique per put so
 *      their bytes cannot mix, but the LAST RENAME WINS -- and it may be the
 *      stale worker's. `Storage.put`'s ownership re-check is what makes the
 *      stale worker decline instead. Mutation: remove that check, and
 *      `the reaped worker's bytes never become the committed file` must go red.
 *
 *   2. A BODY THAT STOPS EARLY. Two mechanisms can catch it and only one of them
 *      is the guard under test, so there are two tests: a real dropped
 *      connection (caught by the transport) and a source that ends cleanly
 *      having delivered less than it advertised (caught only by the byte-count
 *      check). Mutation: remove the check, and the second must go red while the
 *      first STAYS GREEN. Without that pairing a red result would only say "the
 *      line matters somehow" (plan §5 rule 4).
 *
 * EVERY OVERLAP HERE IS FORCED, NOT HOPED FOR. `Promise.all` over two `runOnce`
 * calls proves dispatch, not simultaneity -- the first can rename before the
 * second opens its file, which is the shape that lets both mutations pass. So
 * each writer is gated mid-body and the test ASSERTS two temp files exist at
 * once before releasing anything (plan §5's overlap lesson, and phase 2's
 * journal on the same trap).
 *
 * THE PAYLOADS HAVE DIFFERENT LENGTHS ON PURPOSE. Phase 2 lost a day to two
 * equal-length payloads written at the same offset: the shared file looked
 * uniform and the mutation went flaky-green 3 out of 6 runs. Byte-for-byte
 * equality against ONE of two differently-sized payloads has no such hole.
 */
import { eq } from 'drizzle-orm';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import type { Pool } from 'pg';
import type { CdnClient, CdnResponse } from '../../src/cdn/cdn.client';
import { UndiciCdnClient } from '../../src/cdn/cdn.client';
import * as videosRepository from '../../src/db/videos.repository';
import { jobs, profiles, videos, type Video } from '../../src/db/schema';
import { DownloadLane } from '../../src/lanes/download.lane';
import type { LaneLogger } from '../../src/lanes/lane.runner';
import type { VideoPlayAddr } from '../../src/parser/parser.client';
import { LocalDiskStorage } from '../../src/storage/local-disk.storage';
import {
  applyForwardMigration,
  createEmptySchema,
  createTestPool,
  dropSchema,
  testDb,
  testSchemaName,
  truncateAll,
} from '../support/db';
import { channelCollection } from '../support/collections';
import { FakeCdn } from '../support/fake-cdn';
import { deferred, waitUntil } from '../support/streams';

jest.setTimeout(90_000);

const SCHEMA = testSchemaName('download_race');
const PROFILE = 'MS4wLjABAAAA_dl_race';
const AWEME = 'race-1';
const LEASE_SECONDS = 300;
const MAX_ATTEMPTS = 3;

/**
 * Deliberately different sizes -- see the file comment. `A` is the worker that
 * gets reaped; `B` is its replacement.
 */
const PAYLOAD_A = Buffer.alloc(40_000, 0xaa);
const PAYLOAD_B = Buffer.alloc(23_457, 0xbb);

let pool: Pool;
let db: ReturnType<typeof testDb>;
let root: string;
let storage: LocalDiskStorage;
let cdnClient: UndiciCdnClient;
const cdn = new FakeCdn();

const lines: string[] = [];
const logger: LaneLogger = {
  error: (m) => lines.push(m),
  warn: (m) => lines.push(m),
  log: (m) => lines.push(m),
};

/** Never re-resolves: every test here hands the lane a URL that works. */
const noResolve = {
  async fetchOneVideo(): Promise<VideoPlayAddr> {
    throw new Error('fetchOneVideo must not be called in this suite');
  },
};

const never = new AbortController().signal;

interface LaneOverrides {
  cdn?: CdnClient;
  heartbeatMs?: number;
  maxAttempts?: number;
}

function makeLane(index: number, o: LaneOverrides = {}): DownloadLane {
  return new DownloadLane(logger, db, noResolve, o.cdn ?? cdnClient, storage, {
    index,
    leaseSeconds: LEASE_SECONDS,
    maxAttempts: o.maxAttempts ?? MAX_ATTEMPTS,
    urlTtlSeconds: 7200,
    // A minute, so the heartbeat plays no part unless a test asks for it. The
    // heartbeat is a SEPARATE guard with its own tests below; leaving it on
    // here would mean the commit-race tests could be passing because of it
    // rather than because of the check they name.
    heartbeatMs: o.heartbeatMs ?? 60_000,
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

async function seedVideo(playAddr: string): Promise<void> {
  await db.insert(videos).values({
    awemeId: AWEME,
    secUserId: PROFILE,
    collectionId: await channelCollection(pool, PROFILE),
    createTime: new Date(Date.UTC(2026, 0, 1)),
    playAddr,
  });
  await pool.query("UPDATE videos SET url_expires_at = now() + interval '1 hour'");
}

async function readVideo(awemeId = AWEME): Promise<Video> {
  const [row] = await db.select().from(videos).where(eq(videos.awemeId, awemeId));
  if (!row) throw new Error(`video ${awemeId} not found`);
  return row;
}

async function entriesInKeyDir(): Promise<string[]> {
  try {
    return (await readdir(join(root, 'douyin', PROFILE))).sort();
  } catch {
    return [];
  }
}

async function tempCount(): Promise<number> {
  return (await entriesInKeyDir()).filter((name) => name.endsWith('.tmp')).length;
}

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
  root = await mkdtemp(join(tmpdir(), 'reup-download-race-'));
  storage = new LocalDiskStorage(root);
  cdnClient = new UndiciCdnClient({ timeoutMs: 10_000 });
  cdn.reset();
  lines.length = 0;
});

afterEach(async () => {
  await cdnClient.close();
  await truncateAll(pool);
  await rm(root, { recursive: true, force: true });
});

/**
 * Puts two lanes genuinely mid-download on the SAME video, with the second one
 * holding the claim.
 *
 * The sequence is the real one, not an approximation of it: lane A claims, its
 * lease lapses, the PRODUCTION reaper query hands the row back, lane B claims
 * it -- and A is still streaming throughout. The `play_addr` is swapped between
 * the two claims so the two workers fetch different bytes; that is what makes
 * "whose file got committed" an answerable question.
 *
 * Returns the two in-flight ticks plus the gates that let each body finish, so
 * the caller chooses the commit ORDER. That choice is the whole point: A must
 * be able to rename LAST, or removing the ownership check would still leave B's
 * file in place and the mutation would pass.
 */
async function twoWritersMidFlight(): Promise<{
  tickA: Promise<unknown>;
  tickB: Promise<unknown>;
  finishA: () => void;
  finishB: () => void;
  tokenA: number;
  tokenB: number;
}> {
  const gateA = deferred();
  const gateB = deferred();
  cdn.respondWith((req, res) => {
    const isA = req.url?.startsWith('/A');
    const payload = isA ? PAYLOAD_A : PAYLOAD_B;
    const gate = isA ? gateA.promise : gateB.promise;
    res.writeHead(200, { 'content-length': String(payload.length) });
    res.write(payload.subarray(0, 1024));
    void gate.then(() => res.end(payload.subarray(1024)));
  });

  await seedVideo(cdn.url('/A.mp4'));

  const laneA = makeLane(1);
  const tickA = laneA.runOnce(never);
  await waitUntil(async () => (await tempCount()) === 1, "lane A's write to start");
  const tokenA = (await readVideo()).attempts;

  // The reaper's own query, not a hand-rolled UPDATE: what is under test is
  // what happens after the REAL handover.
  await pool.query("UPDATE videos SET lease_expires_at = now() - interval '1 second'");
  const reaped = await videosRepository.reapOrphans(db, { maxAttempts: MAX_ATTEMPTS });
  expect(reaped.map((row) => row.awemeId)).toEqual([AWEME]);
  // The replacement will be handed a different address, so its bytes differ.
  await pool.query('UPDATE videos SET play_addr = $1', [cdn.url('/B.mp4')]);

  const laneB = makeLane(2);
  const tickB = laneB.runOnce(never);
  await waitUntil(async () => (await tempCount()) === 2, "lane B's write to start");
  const tokenB = (await readVideo()).attempts;

  // THE OVERLAP, asserted rather than assumed: two uncommitted temp files exist
  // at this instant, so both workers really are inside `put`.
  expect(await tempCount()).toBe(2);
  expect(tokenB).not.toBe(tokenA);

  return {
    tickA,
    tickB,
    finishA: gateA.resolve,
    finishB: gateB.resolve,
    tokenA,
    tokenB,
  };
}

describe('the reaper hands a video over mid-download', () => {
  it("the reaped worker's bytes never become the committed file", async () => {
    // THE MUTATION TARGET. Remove the ownership re-check in `Storage.put` (or
    // make the lane's hook always return true) and this must go red: A renames
    // last, so its 40,000 bytes replace B's 23,457 under the same name while
    // the row says `done`. Nothing downstream would ever notice -- the file
    // exists and has a plausible size.
    await seedJob();
    const race = await twoWritersMidFlight();

    // B commits FIRST...
    race.finishB();
    await race.tickB;
    expect(await readFile(join(root, 'douyin', PROFILE, `${AWEME}.mp4`))).toEqual(PAYLOAD_B);

    // ...and A, the reaped worker, finishes afterwards. This ORDER is what
    // makes the mutation detectable: with A renaming last, an unchecked commit
    // overwrites the file that was correct.
    race.finishA();
    await race.tickA;

    const committed = await readFile(join(root, 'douyin', PROFILE, `${AWEME}.mp4`));
    expect(committed).toEqual(PAYLOAD_B);
    expect(committed).not.toEqual(PAYLOAD_A);
    expect(committed.length).toBe(PAYLOAD_B.length);
  });

  it('leaves exactly one file and no temp files behind', async () => {
    // The other half of trap 2 from `storage.interface.ts`: a declined commit
    // that forgot to unlink would leak a FULL-SIZE video per reaped download,
    // invisible to `exists()` and eating the space the phase-10 preflight is
    // promised. Measured on disk, not through a spy.
    await seedJob();
    const race = await twoWritersMidFlight();

    race.finishB();
    await race.tickB;
    race.finishA();
    await race.tickA;

    expect(await entriesInKeyDir()).toEqual([`${AWEME}.mp4`]);
  });

  it('leaves the row exactly as the winning claim left it', async () => {
    await seedJob();
    const race = await twoWritersMidFlight();

    race.finishB();
    await race.tickB;
    const afterB = await readVideo();
    race.finishA();
    await race.tickA;

    const afterA = await readVideo();
    expect(afterA.status).toBe('done');
    expect(afterA.attempts).toBe(race.tokenB);
    expect(afterA.storagePath).toBe(afterB.storagePath);
  });

  it('says out loud that it declined, naming the video', async () => {
    // A silent decline is a download that vanished. The operator-facing signal
    // matters here because the straggler DID spend bandwidth.
    await seedJob();
    const race = await twoWritersMidFlight();

    race.finishB();
    await race.tickB;
    race.finishA();
    await race.tickA;

    expect(lines.join('\n')).toMatch(new RegExp(`declined to commit video ${AWEME}`));
  });

  it('CONTROL: the committed file is a COMPLETE copy of one payload, never a mixture', async () => {
    // Must stay GREEN when the ownership check is removed. Two mechanisms guard
    // this key: the unique temp name (phase 2) stops the bytes MIXING, and the
    // ownership check decides WHOSE complete file wins. A red mutation on the
    // second must not be creditable to the first, and this is how that is told
    // apart -- without it, "the mutation went red" would only say the line
    // matters somehow (plan §5 rule 4).
    await seedJob();
    const race = await twoWritersMidFlight();

    race.finishB();
    await race.tickB;
    race.finishA();
    await race.tickA;

    const committed = await readFile(join(root, 'douyin', PROFILE, `${AWEME}.mp4`));
    const isWholeA = committed.equals(PAYLOAD_A);
    const isWholeB = committed.equals(PAYLOAD_B);
    expect(isWholeA || isWholeB).toBe(true);
  });
});

describe('a cancel that arrives mid-download', () => {
  it('commits nothing, cleans up the temp file, and marks the video skipped', async () => {
    // The DoD line: "Cancel giữa lúc tải → dừng, `.tmp` được dọn, không có file
    // cụt". True BY CONSTRUCTION rather than by timing, because the rename is
    // the only commit point and the cancellation is asked immediately before
    // it: a cancel that lands before the rename cannot leave anything behind,
    // and one that lands after it is not mid-download at all.
    const jobId = await seedJob();
    const gate = deferred();
    cdn.serveGated(PAYLOAD_A, gate.promise);
    await seedVideo(cdn.url('/a.mp4'));

    const lane = makeLane(1);
    const tick = lane.runOnce(never);
    await waitUntil(async () => (await tempCount()) === 1, 'the download to start');

    await pool.query("UPDATE jobs SET status = 'cancelled' WHERE id = $1", [jobId]);
    gate.resolve();
    expect(await tick).toBe('worked');

    const row = await readVideo();
    expect(row.status).toBe('skipped');
    expect(row.lastError).toBeNull();
    expect(row.leaseExpiresAt).toBeNull();
    expect(await entriesInKeyDir()).toEqual([]);
  });

  it('abandons the transfer promptly instead of paying for the rest of it', async () => {
    // Finishing a 200MB download for a job nobody wants is bandwidth spent on
    // nothing. The heartbeat is what notices, so this test turns it up -- and
    // the body here NEVER completes, so a lane that only checked at the commit
    // point would hang here rather than fail, which is its own kind of red.
    const jobId = await seedJob();
    cdn.serveGated(PAYLOAD_A, new Promise(() => {}));
    await seedVideo(cdn.url('/a.mp4'));

    const lane = makeLane(1, { heartbeatMs: 30 });
    const tick = lane.runOnce(never);
    await waitUntil(async () => (await tempCount()) === 1, 'the download to start');
    await pool.query("UPDATE jobs SET status = 'cancelled' WHERE id = $1", [jobId]);

    expect(await tick).toBe('worked');

    expect((await readVideo()).status).toBe('skipped');
    expect(await entriesInKeyDir()).toEqual([]);
  });
});

describe('the heartbeat', () => {
  it('renews the lease while bytes are moving', async () => {
    // The risk register's "File lớn vượt lease giữa lúc tải": a 200MB video on
    // a slow CDN outlives LEASE_SECONDS, and the reaper cannot tell that from a
    // dead worker.
    await seedJob();
    const gate = deferred();
    cdn.serveGated(PAYLOAD_A, gate.promise);
    await seedVideo(cdn.url('/a.mp4'));

    const lane = makeLane(1, { heartbeatMs: 30 });
    const tick = lane.runOnce(never);
    await waitUntil(async () => (await tempCount()) === 1, 'the download to start');
    const atClaim = (await readVideo()).leaseExpiresAt!;

    await waitUntil(
      async () => ((await readVideo()).leaseExpiresAt?.getTime() ?? 0) > atClaim.getTime(),
      'the lease to be renewed mid-download',
    );

    gate.resolve();
    await tick;
    expect((await readVideo()).status).toBe('done');
  });

  it('abandons the transfer when the reaper hands the claim to somebody else', async () => {
    // The other half of the ownership guard, and until now the untested half.
    // The commit check (above) stops a straggler COMMITTING; this stops it
    // paying for the rest of a 200MB download it will not be allowed to commit.
    // With `on.onLost()` deleted the lane keeps streaming a body that never
    // ends, so the failure is a HANG -- which is why the wait below is one this
    // test owns rather than jest's 90-second ceiling (plan §5 rule 6: a hang is
    // a worse signal than a red, so make it red).
    await seedJob();
    cdn.serveGated(PAYLOAD_A, new Promise(() => {}));
    await seedVideo(cdn.url('/a.mp4'));

    const lane = makeLane(1, { heartbeatMs: 30 });
    const tick = lane.runOnce(never);
    await waitUntil(async () => (await tempCount()) === 1, 'the download to start');

    // The real handover: the lease lapses and the PRODUCTION reaper query gives
    // the row back, exactly as the 60s sweep would.
    await pool.query("UPDATE videos SET lease_expires_at = now() - interval '1 second'");
    const reaped = await videosRepository.reapOrphans(db, { maxAttempts: MAX_ATTEMPTS });
    expect(reaped.map((row) => row.awemeId)).toEqual([AWEME]);

    const bounded = new Promise<never>((_resolve, reject) => {
      // `unref`'d so a passing run does not hold the process open for 8s.
      setTimeout(
        () => reject(new Error('the lane never noticed it had lost the claim')),
        8_000,
      ).unref();
    });

    expect(await Promise.race([tick, bounded])).toBe('worked');

    expect(lines.join('\n')).toMatch(new RegExp(`lost the claim on video ${AWEME}`));
    // Nothing committed, and nothing left behind.
    expect(await entriesInKeyDir()).toEqual([]);
    // And it did not touch the row it no longer owns.
    const row = await readVideo();
    expect(row.status).toBe('pending');
    expect(row.lastError).toBeNull();
  });

  it('does NOT renew a lease it is not earning', async () => {
    // A wedged socket must not be able to hold a claim open forever. The
    // renewal is gated on PROGRESS -- the same rule the enumerate checkpoint
    // follows -- so a body that has delivered nothing renews nothing, and the
    // reaper is free to hand the video to somebody who can actually fetch it.
    await seedJob();
    // Headers, then zero body bytes, forever.
    cdn.serveGated(PAYLOAD_A, new Promise(() => {}), 0);
    await seedVideo(cdn.url('/a.mp4'));

    const lane = makeLane(1, { heartbeatMs: 20 });
    const aborter = new AbortController();
    const tick = lane.runOnce(aborter.signal);
    await waitUntil(async () => cdn.downloads.length === 1, 'the request to be made');
    const atClaim = (await readVideo()).leaseExpiresAt!;

    // Ten beats' worth of doing nothing.
    await new Promise((r) => setTimeout(r, 250));
    expect((await readVideo()).leaseExpiresAt!.getTime()).toBe(atClaim.getTime());

    aborter.abort();
    await tick;
  });
});

/**
 * A `CdnClient` that advertises more than it delivers, and then ends CLEANLY.
 *
 * The shape matters. A real dropped connection is caught by the transport (the
 * test below it proves that), so a suite that only had that case would go GREEN
 * with the byte-count check deleted -- the check would be untested code with a
 * comment claiming it was load-bearing. This stub is the case where the count
 * is the ONLY detector, which is what makes the mutation meaningful.
 *
 * It is not a fantasy either: `Content-Length` is a claim made by whatever
 * answered, and an edge, a proxy or a rewritten range response can make that
 * claim wrongly without the socket ever breaking.
 */
class ShortBodyCdn implements CdnClient {
  constructor(
    private readonly payload: Buffer,
    private readonly advertised: number,
  ) {}

  async get(): Promise<CdnResponse> {
    return {
      status: 200,
      contentLength: this.advertised,
      body: Readable.from([this.payload]),
      discard: async () => undefined,
    };
  }

  async close(): Promise<void> {}
}

describe('a body shorter than advertised', () => {
  it('is refused when the source ends cleanly having sent too little', async () => {
    // THE SECOND MUTATION TARGET. Remove the `expectedBytes` check in
    // `local-disk.storage.ts` and this must go red: a 4KB file is committed
    // under the video's name and the row says `done`, and the truncation is
    // discovered when somebody tries to play it.
    await seedJob();
    await seedVideo('http://cdn.invalid/a.mp4');
    const short = new ShortBodyCdn(Buffer.alloc(4096, 3), 4096 + 60_000);

    await expect(makeLane(1, { cdn: short }).runOnce(never)).rejects.toThrow(
      /received 4096 bytes but 64096 were advertised/,
    );

    const row = await readVideo();
    expect(row.status).toBe('pending');
    expect(row.storagePath).toBeNull();
    expect(row.lastError).toMatch(/advertised/);
    expect(await entriesInKeyDir()).toEqual([]);
  });

  it('CONTROL: a real dropped connection is caught by the transport, not by the count', async () => {
    // Must stay GREEN with the byte-count check removed. That is what proves
    // the test above is measuring the count rather than being rescued by the
    // socket error -- the two-mechanisms rule again (plan §5 rule 4).
    await seedJob();
    cdn.serveTruncated(PAYLOAD_A, 4096);
    await seedVideo(cdn.url('/a.mp4'));

    await expect(makeLane(1).runOnce(never)).rejects.toThrow();

    const row = await readVideo();
    expect(row.status).toBe('pending');
    expect(row.storagePath).toBeNull();
    expect(await entriesInKeyDir()).toEqual([]);
  });

  it('accepts a body of exactly the advertised length', async () => {
    // The positive control for both tests above: an instrument that rejected
    // everything would report perfect protection.
    await seedJob();
    cdn.serveBytes(PAYLOAD_B);
    await seedVideo(cdn.url('/a.mp4'));

    expect(await makeLane(1).runOnce(never)).toBe('worked');

    expect((await readVideo()).status).toBe('done');
    expect(await readFile(join(root, 'douyin', PROFILE, `${AWEME}.mp4`))).toEqual(PAYLOAD_B);
  });
});
