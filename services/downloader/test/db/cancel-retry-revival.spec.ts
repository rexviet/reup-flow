/**
 * PHASE 6's HANDOVER: a cancelled job's `skipped` videos had no way back.
 *
 * The `TODO(phase-7)` next to `isProfileCancelled` in jobs.repository.ts spells
 * out the trap, and it is worth restating because the shape is the same one that
 * produced phase 6's worst bug (a zero-byte file marked `done` while every count
 * agreed):
 *
 *   1. cancel a job -> the download lane marks its outstanding videos `skipped`.
 *   2. `cancelled` was not in `RETRYABLE_STATUSES`, so that job could not come
 *      back.
 *   3. `resetFailedForProfile` touched only `failed`, so nothing reset `skipped`.
 *   4. a NEW job on the same profile re-enumerates, and `upsertMany`'s
 *      `ON CONFLICT DO NOTHING` correctly does nothing (F7).
 *   5. `claimOne` only ever looks at `pending`, so those videos are never
 *      downloaded again -- by anybody, ever.
 *
 * AND THE PART THAT MAKES IT DANGEROUS RATHER THAN MERELY ANNOYING: `skipped`
 * counts as SETTLED. `hasWorkForProfile` ignores it and `finalizeSettled`'s
 * `notExists(outstanding)` is satisfied, so the new job walks to `completed`
 * with `downloaded_count = 0` and no error anywhere. A profile that is silently
 * un-downloadable forever, reported as finished. No mutation could have caught
 * that, because there was no guard to mutate -- exactly the lesson phase 6
 * recorded.
 *
 * Phase 7 owns retry/cancel, so it owns this. Two independent ways back, both
 * tested here, because they answer different operator actions:
 *
 *   - `/retry` on the cancelled job -- "actually, carry on with that".
 *   - creating a NEW job on the profile -- "somebody wants this profile again",
 *     which is also what stops the false `completed` above.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import * as jobsRepository from '../../src/db/jobs.repository';
import * as videosRepository from '../../src/db/videos.repository';
import { jobs, profiles, videos, type Video } from '../../src/db/schema';
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

const SCHEMA = testSchemaName('cancel_retry');
const PROFILE = 'MS4wLjABAAAA_cancel_retry';
const MAX_ATTEMPTS = 3;
const LEASE_SECONDS = 300;

let pool: Pool;
let db: ReturnType<typeof testDb>;

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

async function seedVideo(awemeId: string): Promise<void> {
  await pool.query(
    `INSERT INTO videos (aweme_id, sec_user_id, create_time, play_addr, url_expires_at)
     VALUES ($1, $2, now(), 'https://cdn.example/x.mp4', now() + interval '1 hour')`,
    [awemeId, PROFILE],
  );
}

async function readVideo(awemeId: string): Promise<Video> {
  const [row] = await db.select().from(videos).where(eq(videos.awemeId, awemeId));
  if (!row) throw new Error(`video ${awemeId} not found`);
  return row;
}

/**
 * Reaches the `skipped` state THE WAY PRODUCTION DOES: claim the row (which
 * spends an attempt, F6), then `markSkipped` with the claim token.
 *
 * Not an `UPDATE videos SET status = 'skipped'`. The attempt count and the
 * cleared lease are part of the state a revival has to undo, and a hand-written
 * row would quietly have neither -- so the test would be measuring an easier
 * world than the one the fix runs in (plan §5 rule 7).
 */
async function cancelAndSkip(jobId: string, awemeId: string): Promise<void> {
  expect(await jobsRepository.cancel(db, jobId)).not.toBeNull();
  const claimed = await videosRepository.claimOne(db, {
    leaseSeconds: LEASE_SECONDS,
    maxAttempts: MAX_ATTEMPTS,
  });
  expect(claimed?.awemeId).toBe(awemeId);
  // The lane's own check, run for real: a cancelled job's video costs zero bytes.
  expect(await jobsRepository.isProfileCancelled(db, PROFILE)).toBe(true);
  expect(await videosRepository.markSkipped(db, awemeId, claimed!.attempts)).toBe(true);
  expect((await readVideo(awemeId)).status).toBe('skipped');
}

describe('a cancelled job leaves videos that nothing would ever claim again', () => {
  it('is the state the fix has to undo: skipped, an attempt spent, invisible to claimOne', async () => {
    // The precondition, asserted rather than assumed -- because if `claimOne`
    // ever DID pick up a `skipped` row, both fixes below would be solutions to
    // a problem that no longer exists and their tests would pass vacuously.
    const [job] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('skip-1');

    await cancelAndSkip(job!.id, 'skip-1');

    expect((await readVideo('skip-1')).attempts).toBe(1);
    expect(
      await videosRepository.claimOne(db, {
        leaseSeconds: LEASE_SECONDS,
        maxAttempts: MAX_ATTEMPTS,
      }),
    ).toBeNull();
  });

  it('and `skipped` counts as SETTLED, so a new job would report completed with 0 downloads', async () => {
    // The consequence that makes this a data-loss bug rather than an
    // inconvenience, pinned as a fact about the queries rather than as a story:
    // `hasWorkForProfile` and `finalizeSettled` both treat `skipped` as done.
    const [cancelled] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('skip-2');
    await cancelAndSkip(cancelled!.id, 'skip-2');

    // A second job, moved straight to `downloading` (i.e. enumeration found
    // nothing new to add -- F7 -- which is exactly the normal re-run case).
    await pool.query(
      "INSERT INTO jobs (sec_user_id, status, total_videos) VALUES ($1, 'downloading', 1)",
      [PROFILE],
    );

    expect(await videosRepository.hasWorkForProfile(db, PROFILE)).toBe(false);
    const finalized = await jobsRepository.finalizeSettled(db, { secUserId: PROFILE });
    expect(finalized).toHaveLength(1);
    expect(finalized[0]!.status).toBe('completed');
    expect(finalized[0]!.downloadedCount).toBe(0);
  });
});

describe('way back #1 -- /retry on the cancelled job', () => {
  it('accepts a cancelled job, because retry means "actually, carry on"', async () => {
    const [job] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('back-1');
    await cancelAndSkip(job!.id, 'back-1');

    const retried = await jobsRepository.retry(db, job!.id);

    expect(retried).not.toBeNull();
    expect(retried!.status).toBe('pending');
  });

  it('brings the skipped videos back to pending with attempts reset', async () => {
    const [job] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('back-2');
    await cancelAndSkip(job!.id, 'back-2');

    expect(await jobsRepository.retry(db, job!.id)).not.toBeNull();
    const revived = await videosRepository.reviveSkippedForProfile(db, PROFILE);

    expect(revived).toBe(1);
    const video = await readVideo('back-2');
    expect(video.status).toBe('pending');
    // `attempts = 0` is not tidiness: `claimOne` filters on
    // `attempts < maxAttempts`, and a video skipped on its last attempt would
    // otherwise read `pending` in every listing while being invisible to the
    // only query that picks up work -- the identical trap
    // `resetFailedForProfile` documents.
    expect(video.attempts).toBe(0);
    expect(video.leaseExpiresAt).toBeNull();
    // THE claim that matters: something will actually pick it up now.
    const claimed = await videosRepository.claimOne(db, {
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
    });
    expect(claimed?.awemeId).toBe('back-2');
  });

  it('leaves `done` videos alone -- a retry must not re-download the archive', async () => {
    // F7's whole economy. `reviveSkippedForProfile` is scoped to `skipped` for
    // the same reason `upsertMany` is `DO NOTHING`: a re-run must cost only the
    // videos it does not have.
    const [job] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('keep-done');
    await seedVideo('bring-back');
    await pool.query(
      "UPDATE videos SET status = 'done', storage_path = 'x/y.mp4' WHERE aweme_id = 'keep-done'",
    );
    await cancelAndSkip(job!.id, 'bring-back');

    expect(await videosRepository.reviveSkippedForProfile(db, PROFILE)).toBe(1);

    expect((await readVideo('keep-done')).status).toBe('done');
    expect((await readVideo('keep-done')).storagePath).toBe('x/y.mp4');
    expect((await readVideo('bring-back')).status).toBe('pending');
  });

  it('touches nothing on another profile', async () => {
    // Profile-scoped, like every other query over `videos` (F7). A revival that
    // swept globally would resurrect somebody else's deliberate cancel.
    const other = 'MS4wLjABAAAA_cancel_retry_other';
    await db.insert(profiles).values({ secUserId: other }).onConflictDoNothing();
    await pool.query(
      `INSERT INTO videos (aweme_id, sec_user_id, create_time, status)
       VALUES ('other-skipped', $1, now(), 'skipped')`,
      [other],
    );
    const [job] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('mine');
    await cancelAndSkip(job!.id, 'mine');

    expect(await videosRepository.reviveSkippedForProfile(db, PROFILE)).toBe(1);

    expect((await readVideo('other-skipped')).status).toBe('skipped');
  });
});

describe('the race the revival alone cannot close', () => {
  it('refuses to skip a video once a LIVE job wants the profile again', async () => {
    // FOUND BY ADVERSARIAL REVIEW, and it is the phase-6 handover bug rebuilt out
    // of a race rather than out of a missing reset:
    //
    //   1. job A is cancelled.
    //   2. a lane claims video V -- the row is now `downloading`.
    //   3. job B is created. `reviveSkippedForProfile` runs in that transaction
    //      and finds nothing to revive: V is `downloading`, not `skipped`.
    //   4. the lane writes `skipped`. `ownsClaim` is satisfied, so it lands.
    //
    // V is then `skipped` under a LIVE job. `claimOne` never looks at `skipped`,
    // and `skipped` counts as SETTLED -- so job B finalizes as `completed` with V
    // missing and every counter agreeing. No status anywhere looks wrong, which
    // is why neither read the lane already performs can close it: neither is
    // atomic with this write.
    const [cancelled] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('race-1');
    expect(await jobsRepository.cancel(db, cancelled!.id)).not.toBeNull();
    const claimed = await videosRepository.claimOne(db, {
      leaseSeconds: LEASE_SECONDS,
      maxAttempts: MAX_ATTEMPTS,
    });
    expect(claimed?.awemeId).toBe('race-1');

    // Job B arrives while the lane holds the claim -- exactly step 3.
    await db.insert(jobs).values({ secUserId: PROFILE });

    // The write must not land.
    expect(await videosRepository.markSkipped(db, 'race-1', claimed!.attempts)).toBe(false);
    // The row is left where the reaper (F3) can hand it back, NOT stranded.
    expect((await readVideo('race-1')).status).toBe('downloading');
    expect(await videosRepository.hasWorkForProfile(db, PROFILE)).toBe(true);
  });

  it('still skips normally while the cancel is the only thing asking', async () => {
    // The other half, so the guard above cannot be satisfied by a `markSkipped`
    // that has simply stopped working. Without this the mutation "make
    // markSkipped always return false" would look like a correct implementation.
    const [cancelled] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('race-2');

    await cancelAndSkip(cancelled!.id, 'race-2');

    expect((await readVideo('race-2')).status).toBe('skipped');
  });
});

describe('way back #2 -- a new job on the profile', () => {
  it('revives the skipped videos, so the new job has real work rather than a false completion', async () => {
    // The half that closes the silent-`completed` hole above. Somebody asking
    // for this profile again means nobody's earlier cancel applies any more,
    // and the videos become work.
    const [cancelled] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('newjob-1');
    await cancelAndSkip(cancelled!.id, 'newjob-1');

    await db.transaction(async (tx) => {
      await jobsRepository.upsertProfile(tx, PROFILE);
      await jobsRepository.create(tx, PROFILE);
      await videosRepository.reviveSkippedForProfile(tx, PROFILE);
    });

    expect((await readVideo('newjob-1')).status).toBe('pending');
    // And now the profile DOES owe work, so `finalizeSettled` cannot walk the
    // new job to `completed` behind everyone's back.
    expect(await videosRepository.hasWorkForProfile(db, PROFILE)).toBe(true);
    await pool.query("UPDATE jobs SET status = 'downloading' WHERE status = 'pending'");
    expect(await jobsRepository.finalizeSettled(db, { secUserId: PROFILE })).toHaveLength(0);
  });

  it('and the live job means isProfileCancelled goes back to false', async () => {
    // The conjunction `isProfileCancelled` is built on ("somebody cancelled AND
    // nobody else is still asking") doing its job: the revived videos must not
    // be re-skipped by the very next lane tick.
    const [cancelled] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
    await seedVideo('newjob-2');
    await cancelAndSkip(cancelled!.id, 'newjob-2');
    expect(await jobsRepository.isProfileCancelled(db, PROFILE)).toBe(true);

    await db.insert(jobs).values({ secUserId: PROFILE });

    expect(await jobsRepository.isProfileCancelled(db, PROFILE)).toBe(false);
  });
});
