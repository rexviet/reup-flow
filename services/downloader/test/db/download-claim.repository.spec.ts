/**
 * Phase 6 -- the four database questions the download lane asks that phase 1
 * did not need.
 *
 * All against a real Postgres, for phase 1's reason: every one of these is a
 * statement whose whole point is what the DATABASE clock says, or what happens
 * when two statements race. A fake would prove the fake agrees with itself.
 *
 *   holdsClaim          the pre-rename ownership check (risk register: "Reaper
 *                       nhả video, 2 worker commit 1 file")
 *   renewLease          the heartbeat, so a video larger than one lease is not
 *                       reaped out from under a live download
 *   isUrlFresh          F1: is the cached play_addr still inside URL_TTL?
 *   saveResolvedUrl     F1's reactive re-resolve, writing the new URL back
 *   markSkipped         a cancelled job's videos are not failures
 *   hasWorkForProfile   step 8: is this profile finished?
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import {
  claimOne,
  hasWorkForProfile,
  holdsClaim,
  isUrlFresh,
  markFailed,
  markSkipped,
  release,
  renewLease,
  saveResolvedUrl,
} from '../../src/db/videos.repository';
import { videos, type Video } from '../../src/db/schema';
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

const SCHEMA = testSchemaName('download_claim');
const OPTS = { leaseSeconds: 300, maxAttempts: 3 };
const PROFILE = 'MS4wLjABAAAA_dl_claim';

let pool: Pool;
let db: ReturnType<typeof testDb>;

async function seedPending(ids: string[], secUserId = PROFILE): Promise<void> {
  await db.insert(videos).values(
    ids.map((awemeId, i) => ({
      awemeId,
      secUserId,
      createTime: new Date(Date.UTC(2026, 0, 1) + i * 60_000),
    })),
  );
}

async function readVideo(awemeId: string): Promise<Video> {
  const [row] = await db.select().from(videos).where(eq(videos.awemeId, awemeId));
  if (!row) throw new Error(`video ${awemeId} not found`);
  return row;
}

async function expireLease(awemeId: string): Promise<void> {
  await pool.query(
    "UPDATE videos SET lease_expires_at = now() - interval '1 hour' WHERE aweme_id = $1",
    [awemeId],
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

describe('holdsClaim -- the pre-rename ownership check', () => {
  it('is true for the claim it was handed', async () => {
    await seedPending(['a']);
    const claimed = (await claimOne(db, OPTS))!;

    expect(await holdsClaim(db, claimed.awemeId, claimed.attempts)).toBe(true);
  });

  it('is false once the lease has expired', async () => {
    // The reaper has not run yet, but the lease is gone: whoever holds this row
    // is out of time and must not commit over a replacement that may already
    // have started.
    await seedPending(['a']);
    const claimed = (await claimOne(db, OPTS))!;
    await expireLease('a');

    expect(await holdsClaim(db, claimed.awemeId, claimed.attempts)).toBe(false);
  });

  it('is false after the row has been reaped and re-claimed by someone else', async () => {
    // THE case this exists for. `attempts` is the claim token: F6 increments it
    // at claim time, so a re-claim necessarily carries a different number and a
    // straggler's number no longer matches.
    await seedPending(['a']);
    const first = (await claimOne(db, OPTS))!;
    await expireLease('a');
    await pool.query("UPDATE videos SET status = 'pending', lease_expires_at = NULL");
    const second = (await claimOne(db, OPTS))!;

    expect(second.attempts).not.toBe(first.attempts);
    expect(await holdsClaim(db, 'a', second.attempts)).toBe(true);
    expect(await holdsClaim(db, 'a', first.attempts)).toBe(false);
  });

  it('is false once the row has left `downloading` at all', async () => {
    await seedPending(['a']);
    const claimed = (await claimOne(db, OPTS))!;
    await pool.query("UPDATE videos SET status = 'cancelled-ish'");

    expect(await holdsClaim(db, claimed.awemeId, claimed.attempts)).toBe(false);
  });

  it('is false for a row that does not exist', async () => {
    expect(await holdsClaim(db, 'nope', 1)).toBe(false);
  });
});

describe('renewLease -- the heartbeat', () => {
  it('pushes the lease further out and says it still owns the claim', async () => {
    await seedPending(['a']);
    const claimed = (await claimOne(db, OPTS))!;
    const before = (await readVideo('a')).leaseExpiresAt!;

    // 600 rather than 300 so the move is unambiguous even on a fast machine.
    expect(await renewLease(db, 'a', claimed.attempts, 600)).toBe(true);

    expect((await readVideo('a')).leaseExpiresAt!.getTime()).toBeGreaterThan(before.getTime());
  });

  it('refuses to renew a lease that has already expired', async () => {
    // A worker whose lease lapsed has already lost the row in every sense that
    // matters: the reaper may be handing it over right now. Renewing would take
    // it back from underneath a replacement.
    await seedPending(['a']);
    const claimed = (await claimOne(db, OPTS))!;
    await expireLease('a');

    expect(await renewLease(db, 'a', claimed.attempts, 600)).toBe(false);
  });

  it('refuses to renew somebody else\'s claim', async () => {
    await seedPending(['a']);
    const first = (await claimOne(db, OPTS))!;
    await expireLease('a');
    await pool.query("UPDATE videos SET status = 'pending', lease_expires_at = NULL");
    const second = (await claimOne(db, OPTS))!;
    const held = (await readVideo('a')).leaseExpiresAt!;

    expect(await renewLease(db, 'a', first.attempts, 600)).toBe(false);
    // And it really did not move the replacement's lease.
    expect((await readVideo('a')).leaseExpiresAt!.getTime()).toBe(held.getTime());
    expect(await renewLease(db, 'a', second.attempts, 600)).toBe(true);
  });
});

describe('isUrlFresh -- F1', () => {
  it('is true while url_expires_at is still in the future', async () => {
    await seedPending(['a']);
    await pool.query(
      "UPDATE videos SET play_addr = 'https://cdn/x.mp4', url_expires_at = now() + interval '1 hour'",
    );

    expect(await isUrlFresh(db, 'a')).toBe(true);
  });

  it('is false once url_expires_at has passed', async () => {
    await seedPending(['a']);
    await pool.query(
      "UPDATE videos SET play_addr = 'https://cdn/x.mp4', url_expires_at = now() - interval '1 second'",
    );

    expect(await isUrlFresh(db, 'a')).toBe(false);
  });

  it('is false when there is no cached URL at all, whatever the timestamp says', async () => {
    // A row with an expiry but no address is not "fresh", it is unusable. Left
    // to a plain timestamp comparison this would send the lane to GET
    // `undefined`.
    await seedPending(['a']);
    await pool.query("UPDATE videos SET url_expires_at = now() + interval '1 hour'");

    expect(await isUrlFresh(db, 'a')).toBe(false);
  });

  it('is false when url_expires_at is null', async () => {
    await seedPending(['a']);
    await pool.query("UPDATE videos SET play_addr = 'https://cdn/x.mp4'");

    expect(await isUrlFresh(db, 'a')).toBe(false);
  });

  it('is judged by the DATABASE clock, not the caller\'s', async () => {
    // Same rule as every lease in this schema (schema.ts's `tstz` note): two
    // lane hosts with a few seconds of skew must not disagree about whether a
    // cached URL is usable. Proven by making the row's expiry a value only
    // Postgres can compare correctly -- one second out.
    await seedPending(['a', 'b']);
    await pool.query(
      "UPDATE videos SET play_addr = 'https://cdn/x.mp4', url_expires_at = now() + interval '1 second' WHERE aweme_id = 'a'",
    );
    await pool.query(
      "UPDATE videos SET play_addr = 'https://cdn/x.mp4', url_expires_at = now() - interval '1 second' WHERE aweme_id = 'b'",
    );

    expect(await isUrlFresh(db, 'a')).toBe(true);
    expect(await isUrlFresh(db, 'b')).toBe(false);
  });
});

describe('saveResolvedUrl -- the reactive re-resolve writing back', () => {
  it('stores the new address and a fresh TTL measured from the database clock', async () => {
    await seedPending(['a']);
    await pool.query(
      "UPDATE videos SET play_addr = 'https://old/x.mp4', url_expires_at = now() - interval '1 day'",
    );

    await saveResolvedUrl(db, 'a', 'https://new/y.mp4', 3600);

    const row = await readVideo('a');
    expect(row.playAddr).toBe('https://new/y.mp4');
    expect(await isUrlFresh(db, 'a')).toBe(true);
    expect(row.urlExpiresAt!.getTime()).toBeGreaterThan(Date.now() + 3_000_000);
  });

  it('does not touch status, attempts or the lease', async () => {
    // It is called mid-download, on a row this worker is holding. Anything else
    // it wrote would be a second, invisible writer on the claim.
    await seedPending(['a']);
    const claimed = (await claimOne(db, OPTS))!;
    const before = await readVideo('a');

    await saveResolvedUrl(db, 'a', 'https://new/y.mp4', 3600);

    const after = await readVideo('a');
    expect(after.status).toBe('downloading');
    expect(after.attempts).toBe(claimed.attempts);
    expect(after.leaseExpiresAt!.getTime()).toBe(before.leaseExpiresAt!.getTime());
  });
});

describe('markSkipped -- a cancelled job is not a failure', () => {
  it('moves the claim to `skipped` and drops the lease', async () => {
    await seedPending(['a']);
    const claimed = (await claimOne(db, OPTS))!;

    expect(await markSkipped(db, 'a', claimed.attempts)).toBe(true);

    const row = await readVideo('a');
    expect(row.status).toBe('skipped');
    expect(row.leaseExpiresAt).toBeNull();
    // NOT an error. `last_error` is what an operator reads to decide whether
    // something is broken, and a cancelled job is not broken.
    expect(row.lastError).toBeNull();
  });

  it('refuses to skip a claim it no longer holds', async () => {
    await seedPending(['a']);
    const first = (await claimOne(db, OPTS))!;
    await expireLease('a');
    await pool.query("UPDATE videos SET status = 'pending', lease_expires_at = NULL");
    await claimOne(db, OPTS);

    expect(await markSkipped(db, 'a', first.attempts)).toBe(false);
    expect((await readVideo('a')).status).toBe('downloading');
  });
});

describe('hasWorkForProfile -- step 8, "is this profile finished?"', () => {
  it('is true while anything is pending or downloading', async () => {
    await seedPending(['a', 'b']);
    expect(await hasWorkForProfile(db, PROFILE)).toBe(true);

    await claimOne(db, OPTS);
    expect(await hasWorkForProfile(db, PROFILE)).toBe(true);
  });

  it('is false once every video has settled -- done, failed or skipped', async () => {
    await seedPending(['a', 'b', 'c']);
    await pool.query("UPDATE videos SET status = 'done' WHERE aweme_id = 'a'");
    await pool.query("UPDATE videos SET status = 'failed' WHERE aweme_id = 'b'");
    await pool.query("UPDATE videos SET status = 'skipped' WHERE aweme_id = 'c'");

    expect(await hasWorkForProfile(db, PROFILE)).toBe(false);
  });

  it('is scoped to the profile, never to the whole table', async () => {
    // F7: `videos` has no `job_id`, so the profile IS the scope. Counting the
    // whole table would leave every job waiting for every other profile.
    await seedPending(['a'], PROFILE);
    await seedPending(['z'], 'MS4wLjABAAAA_someone_else');
    await pool.query("UPDATE videos SET status = 'done' WHERE aweme_id = 'a'");

    expect(await hasWorkForProfile(db, PROFILE)).toBe(false);
    expect(await hasWorkForProfile(db, 'MS4wLjABAAAA_someone_else')).toBe(true);
  });
});

describe('giving a claim back, gated on the claim token', () => {
  /**
   * `lane.runner.ts` commissioned this as phase-6 work in as many words:
   * `release` runs after the drain, the drain can TIME OUT, so in the worst case
   * it runs while a tick is still in flight -- and an unguarded `UPDATE ... SET
   * status = 'pending'` would then hand back a row a DIFFERENT worker has since
   * claimed. "Today that window is closed by arithmetic ... but arithmetic is
   * not a guard."
   */
  it('release refuses to hand back a claim that is no longer ours', async () => {
    await seedPending(['a']);
    const first = (await claimOne(db, OPTS))!;
    await expireLease('a');
    await pool.query("UPDATE videos SET status = 'pending', lease_expires_at = NULL");
    const second = (await claimOne(db, OPTS))!;

    expect(await release(db, 'a', { ...OPTS, attempts: first.attempts })).toBe(false);
    // Untouched: still `downloading` under the replacement's claim.
    expect((await readVideo('a')).status).toBe('downloading');

    expect(await release(db, 'a', { ...OPTS, attempts: second.attempts })).toBe(true);
    expect((await readVideo('a')).status).toBe('pending');
  });

  it('markFailed refuses to record a failure against somebody else\'s claim', async () => {
    await seedPending(['a']);
    const first = (await claimOne(db, OPTS))!;
    await expireLease('a');
    await pool.query("UPDATE videos SET status = 'pending', lease_expires_at = NULL");
    await claimOne(db, OPTS);

    expect(await markFailed(db, 'a', 'stale worker error', { ...OPTS, attempts: first.attempts })).toBe(
      false,
    );
    const row = await readVideo('a');
    expect(row.status).toBe('downloading');
    // And it did not smear its error over the live claim's row either.
    expect(row.lastError).toBeNull();
  });

  it('refuses when the lease has lapsed, even with the right token', async () => {
    // By then the reaper may be handing the row over right now. `false` is the
    // caller's cue that the give-back did not land, so it can say so rather
    // than logging a release that never happened.
    await seedPending(['a']);
    const claimed = (await claimOne(db, OPTS))!;
    await expireLease('a');

    expect(await release(db, 'a', { ...OPTS, attempts: claimed.attempts })).toBe(false);
    expect((await readVideo('a')).status).toBe('downloading');
  });

  it('is UNCONDITIONAL when no token is supplied -- the reaper path', async () => {
    // The reaper and the phase-4 shutdown harness act on rows whose owner is
    // gone, so there is no live claim to match. Both forms have to exist.
    await seedPending(['a']);
    await claimOne(db, OPTS);
    await expireLease('a');

    expect(await release(db, 'a', OPTS)).toBe(true);
    expect((await readVideo('a')).status).toBe('pending');
  });
});
