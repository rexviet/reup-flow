/**
 * 图文 IMAGE POSTS MUST NOT BE DOWNLOADED, AND MUST NOT DISAPPEAR EITHER.
 *
 * THE BUG, from a real run of a 215-item profile: an image post has no video,
 * and Douyin still fills `video.play_addr.url_list` -- with the BACKGROUND MUSIC
 * CDN. `toVideoUpsert` never looked at what kind of post it was, so the download
 * lane fetched an MP3, wrote it to `<aweme_id>.mp4`, matched it against
 * `content-length`, renamed it into place and marked the row `done`. The run
 * reported 215/215 completed, zero empty files, zero leftover `.tmp`, and two of
 * the files on disk were `MPEG ADTS, layer III, 128 kbps`. Every counter agreed
 * with every other counter. The image content was never saved at all.
 *
 * That is the same family as phase 6's zero-byte commit and phase 7's
 * skipped-under-a-live-job: the archive is wrong while every number is right. So
 * the assertions here are deliberately NOT "the mapper returns the string
 * skipped". They are:
 *
 *   1. the row EXISTS (dropping the item would take it out of `total_videos`
 *      and the profile would read 213/213 with the posts gone -- the same
 *      silence, differently shaped);
 *   2. `claimOne` -- the real query, against real Postgres -- CANNOT reach it,
 *      which is the only statement that means "no MP3 will be downloaded";
 *   3. the real video from the same run is still claimable, because a guard
 *      that skips too much is a worse bug than the one being fixed.
 *
 * Every payload comes from `test/fixtures/aweme/` -- complete, unedited
 * `fetch_one_video` envelopes captured from the parser (plan §5 rule 7: a
 * fixture easier than reality is its own kind of false green). The synthetic
 * items further down exist only to pin WHICH FIELD the decision is made on, and
 * each is the real item with one field changed.
 */
import { eq } from 'drizzle-orm';
import type { Pool } from 'pg';
import { jobs, profiles, videos, type Job, type Video } from '../../src/db/schema';
import { EnumerateLane, type EnumerateParser } from '../../src/lanes/enumerate.lane';
import { toVideoUpsert } from '../../src/lanes/aweme.mapper';
import { claimOne, reviveSkippedForProfile } from '../../src/db/videos.repository';
import type { Aweme, UserPostPage, UserProfile } from '../../src/parser/parser.client';
import {
  IMAGE_POST_IDS,
  VIDEO_ID,
  imagePostAweme,
  videoAweme,
} from '../support/aweme-fixtures';
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

const SCHEMA = testSchemaName('imagepost');
const PROFILE = 'MS4wLjABAAAA_imagepost';
const LEASE_SECONDS = 300;
const URL_TTL_SECONDS = 7200;
const MAX_ATTEMPTS = 3;
const CLAIM_OPTS = { leaseSeconds: LEASE_SECONDS, maxAttempts: MAX_ATTEMPTS };

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
/* The fixtures really are what the rest of this file claims                   */
/* -------------------------------------------------------------------------- */

/**
 * These four tests assert nothing about the code. They assert about the
 * EVIDENCE, and without them every result below is conditional on a fixture
 * nobody re-read.
 *
 * The specific way this file could go quietly vacuous: somebody "tidies" the
 * fixtures -- trims the 148-key items down, or regenerates them by hand -- and
 * the image posts lose their `images` array. The mapper would then classify them
 * as videos, which is the bug, and the tests below would still pass on the
 * synthetic items further down while the real-payload tests turned into
 * assertions about a file that no longer resembles Douyin.
 */
describe('the captured payloads', () => {
  it('are complete Douyin items, not a hand-written idea of one', () => {
    // 146-149 top-level keys. A trimmed fixture is a different, easier world.
    for (const n of [0, 1, 2] as const) {
      expect(Object.keys(imagePostAweme(n)).length).toBeGreaterThan(100);
    }
    expect(Object.keys(videoAweme()).length).toBeGreaterThan(100);
  });

  it('carry the image content as a non-empty `images` array on the image posts', () => {
    for (const n of [0, 1, 2] as const) {
      const images = imagePostAweme(n).images;
      expect(Array.isArray(images)).toBe(true);
      expect((images as unknown[]).length).toBeGreaterThan(0);
      // Each entry is a real image, with its own CDN URLs -- this is the direct
      // evidence the discriminator rests on, so its shape is pinned too.
      const first = (images as Record<string, unknown>[])[0]!;
      expect(Array.isArray(first.url_list)).toBe(true);
      expect((first.url_list as string[])[0]).toMatch(/^https:\/\/[^/]*douyinpic\.com\//);
      expect(typeof first.width).toBe('number');
    }
  });

  it('include the image post whose play_addr is NOT an .mp3 and NOT on a music host', () => {
    // The one that makes the guard's choice of field a measurement rather than a
    // preference. The run was reported as "2 of 215 files are MP3s" because
    // `file(1)` says so for exactly two; a byte sweep of all 215 found SIX audio
    // files -- two starting `ID3`, four starting `ftypM4A`, which is audio inside
    // an MP4 container and which `file(1)` calls MP4. All six are image posts
    // (`aweme_type 68`, `media_type 2`, `images` 5-9 entries, confirmed against
    // the parser). FOUR of the six look like this fixture, so a guard keyed on
    // the URL would have fixed two and reported success.
    const url = (
      ((imagePostAweme(2).video as Record<string, unknown>).play_addr as Record<string, unknown>)
        .url_list as string[]
    )[0]!;

    expect(url).not.toMatch(/\.mp3/);
    expect(url).not.toMatch(/music/);
    expect(url).toMatch(/douyinstatic\.com/);
  });

  it('carry NO images on the video, so the control is a genuine negative', () => {
    // `null`, not `[]` and not absent -- the real thing Douyin sends.
    expect(videoAweme().images).toBeNull();
  });

  it('point `video.play_addr` at AUDIO on an image post and at the video CDN on the video', () => {
    // This is the fact that made the bug possible, and it is asserted here
    // rather than relied on: if Douyin ever stops filling `play_addr` for image
    // posts, the shape of this whole problem changes and this test says so.
    const audioUrls = [0, 1].map(
      (n) =>
        (
          (imagePostAweme(n as 0 | 1).video as Record<string, unknown>)
            .play_addr as Record<string, unknown>
        ).url_list as string[],
    );
    for (const urls of audioUrls) {
      expect(urls[0]).toMatch(/\.mp3(\?|$)/);
      expect(urls[0]).toMatch(/douyinstatic\.com/);
    }

    const videoUrl = (
      ((videoAweme().video as Record<string, unknown>).play_addr as Record<string, unknown>)
        .url_list as string[]
    )[0]!;
    expect(videoUrl).toMatch(/douyinvod\.com/);
    expect(videoUrl).not.toMatch(/\.mp3/);
  });

  it('differ in duration and in the type enums, none of which the guard uses', () => {
    // Pinned so the "we do not key on these" tests below are provably about
    // fields that really do differ in the real payloads.
    expect((imagePostAweme(0).video as Record<string, unknown>).duration).toBe(0);
    expect((videoAweme().video as Record<string, unknown>).duration).toBe(16_950);
    expect(imagePostAweme(0).aweme_type).toBe(68);
    expect(imagePostAweme(0).media_type).toBe(2);
    expect(videoAweme().aweme_type).toBe(55);
    expect(videoAweme().media_type).toBe(4);
  });
});

/* -------------------------------------------------------------------------- */
/* The mapper's verdict on the real items                                      */
/* -------------------------------------------------------------------------- */

describe('the mapper, on the two posts that came back as MP3s', () => {
  it.each([0, 1, 2] as const)('records image post %i as skipped, not pending', (n) => {
    const row = toVideoUpsert(imagePostAweme(n), PROFILE);

    expect(row).not.toBeNull();
    expect(row!.status).toBe('skipped');
  });

  it.each([0, 1, 2] as const)('KEEPS image post %i as a row rather than dropping it', (n) => {
    // The whole point of `skipped` over `null`. A dropped item leaves
    // `total_videos` reading 213 for a 215-item profile, the posts are in no
    // listing, and nothing anywhere is inconsistent -- which is precisely the
    // shape of failure this change exists to end.
    const row = toVideoUpsert(imagePostAweme(n), PROFILE);

    expect(row!.awemeId).toBe(IMAGE_POST_IDS[n]);
    expect(row!.secUserId).toBe(PROFILE);
    expect(row!.createTime.getTime()).toBeGreaterThan(0);
  });

  it.each([0, 1, 2] as const)('says WHY on image post %i, unlike a cancelled-job skip', (n) => {
    // `markSkipped` leaves `last_error` null on purpose: a cancelled video is
    // skipped until somebody asks again, so there is nothing to explain. This
    // skip never resolves -- no file will ever appear for this row -- so the row
    // has to carry the reason. Asserted as "there is one", not as its wording:
    // pinning the prose would make a reword look like a broken guard.
    const reason = toVideoUpsert(imagePostAweme(n), PROFILE)!.lastError;

    expect(typeof reason).toBe('string');
    expect((reason as string).length).toBeGreaterThan(0);
  });
});

describe('the mapper, on the real video from the same run', () => {
  it('leaves it pending and downloadable', () => {
    // The control in the strict sense: a guard that skips too much loses the
    // whole archive, which is worse than the bug it fixes.
    const row = toVideoUpsert(videoAweme(), PROFILE);

    expect(row).not.toBeNull();
    expect(row!.status).toBe('pending');
    expect(row!.lastError).toBeNull();
    expect(row!.awemeId).toBe(VIDEO_ID);
    expect(row!.playAddr).toMatch(/douyinvod\.com/);
    expect(row!.durationMs).toBe(16_950);
  });
});

/* -------------------------------------------------------------------------- */
/* WHICH FIELD the decision is made on                                         */
/* -------------------------------------------------------------------------- */

/**
 * The real image posts differ from the real video in five ways at once
 * (`images`, `aweme_type`, `media_type`, `duration`, the CDN hostname), so
 * "skipped the image post" on its own does not say WHICH of them was read. Each
 * test here is the real item with ONE field moved, which is the only way to tell.
 */
describe('the discriminator is `images`, and nothing else', () => {
  const withField = (base: Aweme, patch: Record<string, unknown>): Aweme => ({
    ...base,
    ...patch,
  });

  it('still skips an image post when Douyin moves the type enums to values we have never seen', () => {
    // `media_type`/`aweme_type` are enums on somebody else's API. Douyin adds
    // values (live replays, collections, mixed posts); a guard keyed on the enum
    // starts downloading MP3s again the day a new number appears, and it fails
    // exactly the way this bug failed -- with every counter in agreement.
    const moved = withField(imagePostAweme(0), { media_type: 9, aweme_type: 999 });

    expect(toVideoUpsert(moved, PROFILE)!.status).toBe('skipped');
  });

  it('still skips an image post served from a hostname we have never seen', () => {
    // The CDN host is infrastructure, not contract, and it is regionalised
    // (`lf26-music-east`, `sf6-cdn-tos`). Keyed on the hostname, this guard
    // breaks towards skipping REAL VIDEOS the first time a bucket moves.
    const base = imagePostAweme(0);
    const rehosted = withField(base, {
      video: {
        ...(base.video as Record<string, unknown>),
        play_addr: { url_list: ['https://brand-new-host.example.cn/obj/thing.mp3'] },
      },
    });

    expect(toVideoUpsert(rehosted, PROFILE)!.status).toBe('skipped');
  });

  it('does NOT skip a video merely because its duration reads 0', () => {
    // A consequence, not the fact -- and also true of a real video whose `video`
    // object arrived malformed. Skipping on it loses real videos silently.
    const base = videoAweme();
    const zeroed = withField(base, {
      video: { ...(base.video as Record<string, unknown>), duration: 0 },
    });

    expect(toVideoUpsert(zeroed, PROFILE)!.status).toBe('pending');
  });

  it('does NOT skip a video merely because its play_addr is on the music CDN', () => {
    const base = videoAweme();
    const musicUrl = (
      ((imagePostAweme(0).video as Record<string, unknown>).play_addr as Record<string, unknown>)
        .url_list as string[]
    )[0]!;
    const rehosted = withField(base, {
      video: {
        ...(base.video as Record<string, unknown>),
        play_addr: { url_list: [musicUrl] },
      },
    });

    expect(toVideoUpsert(rehosted, PROFILE)!.status).toBe('pending');
  });

  it('treats an EMPTY images array as "there are none", the same as a video', () => {
    // `images: []` is Douyin saying there is no image content. Reading it as
    // evidence would skip real videos -- the same silent loss, other direction.
    const empty = withField(videoAweme(), { images: [] });

    expect(toVideoUpsert(empty, PROFILE)!.status).toBe('pending');
    expect(toVideoUpsert(withField(videoAweme(), { images: null }), PROFILE)!.status).toBe(
      'pending',
    );
    expect(toVideoUpsert(withField(videoAweme(), { images: undefined }), PROFILE)!.status).toBe(
      'pending',
    );
  });

  it('does not mistake a non-array `images` for image content', () => {
    // If Douyin ever sends an object here, `Array.isArray` says no and the item
    // stays downloadable. Recorded because the failure direction matters: this
    // errs towards downloading, which is visible, rather than towards skipping,
    // which is not.
    const odd = withField(videoAweme(), { images: { count: 5 } });

    expect(toVideoUpsert(odd, PROFILE)!.status).toBe('pending');
  });

  it('skips a post whose ONLY image-post marker is `images` (nothing else moved)', () => {
    // The mirror of the four tests above: the real video, unchanged except that
    // it now carries image content. Enums, duration and hostname all still say
    // "video". If this went green with the guard removed, the guard would be
    // resting on one of them.
    const withImages = withField(videoAweme(), {
      images: [{ url_list: ['https://p3-pc-sign.douyinpic.com/whatever.webp'], width: 1440 }],
    });

    expect(toVideoUpsert(withImages, PROFILE)!.status).toBe('skipped');
  });
});

/* -------------------------------------------------------------------------- */
/* End to end: the real lane, the real repository, real Postgres               */
/* -------------------------------------------------------------------------- */

/** A one-page profile made of the three real items. */
class OnePageSource implements EnumerateParser {
  readonly calls: number[] = [];

  constructor(private readonly items: Aweme[]) {}

  async fetchUserProfile(): Promise<UserProfile> {
    throw new Error('the F4 probe must not be reached on a healthy page');
  }

  async fetchUserPostPage(_secUserId: string, maxCursor: number): Promise<UserPostPage> {
    this.calls.push(maxCursor);
    return {
      awemeList: this.items,
      hasMore: false,
      maxCursor: 1_735_689_600_000,
      statusCode: 0,
    };
  }
}

function laneFor(source: EnumerateParser): EnumerateLane {
  return new EnumerateLane(recordingLogger, db, source, {
    leaseSeconds: LEASE_SECONDS,
    urlTtlSeconds: URL_TTL_SECONDS,
    sleepMsMin: 1,
    sleepMsMax: 2,
    probeSecUserId: 'MS4wLjABAAAA_probe_target',
  });
}

/** One page of real items: three image posts and the one real video. */
function realPage(): Aweme[] {
  return [imagePostAweme(0), videoAweme(), imagePostAweme(1), imagePostAweme(2)];
}

/** How many rows one `realPage()` produces, and how many of them are skipped. */
const PAGE_ROWS = 4;
const PAGE_SKIPPED = 3;

async function seedJob(): Promise<string> {
  const [row] = await db.insert(jobs).values({ secUserId: PROFILE }).returning();
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

describe('DoD: a profile of three image posts and one video, enumerated for real', () => {
  it('stores all four rows -- the posts are counted, not lost', async () => {
    const id = await seedJob();

    await expect(laneFor(new OnePageSource(realPage())).runOnce(fresh())).resolves.toBe('worked');

    const rows = await allVideos();
    expect(rows).toHaveLength(PAGE_ROWS);
    expect(rows.map((r) => r.awemeId).sort()).toEqual([VIDEO_ID, ...IMAGE_POST_IDS].sort());
    // `total_videos` is what an operator watches. 4, not 1: the image posts are
    // part of this profile and saying otherwise is the silence this change
    // exists to remove.
    const job = await readJob(id);
    expect(job.totalVideos).toBe(PAGE_ROWS);
    expect(job.enumeratedCount).toBe(PAGE_ROWS);
    expect(job.status).toBe('downloading');
  });

  it('puts the image posts in `skipped` and only the video in `pending`', async () => {
    await seedJob();

    await laneFor(new OnePageSource(realPage())).runOnce(fresh());

    const byId = new Map((await allVideos()).map((r) => [r.awemeId, r]));
    expect(byId.get(VIDEO_ID)!.status).toBe('pending');
    for (const id of IMAGE_POST_IDS) {
      expect(byId.get(id)!.status).toBe('skipped');
      expect(byId.get(id)!.lastError).not.toBeNull();
    }
  });

  it('makes the image posts UNREACHABLE by the real claim query -- no MP3 can be fetched', async () => {
    // The assertion that actually means "the bug is gone". `claimOne` is the one
    // query the download lane uses; if it cannot see these rows, no request is
    // ever made for their `play_addr`, which is the music CDN.
    await seedJob();
    await laneFor(new OnePageSource(realPage())).runOnce(fresh());

    const first = await claimOne(db, CLAIM_OPTS);
    const second = await claimOne(db, CLAIM_OPTS);

    expect(first).not.toBeNull();
    expect(first!.awemeId).toBe(VIDEO_ID);
    expect(first!.playAddr).toMatch(/douyinvod\.com/);
    // And nothing else is claimable, on this or any later tick.
    expect(second).toBeNull();
  });

  it('says so in the log, so 215 items and 213 files is not a subtraction', async () => {
    await seedJob();

    await laneFor(new OnePageSource(realPage())).runOnce(fresh());

    expect(logged.join('\n')).toMatch(/3 image post\(s\) skipped/);
  });

  it('re-enumerating the same profile does not resurrect them (ON CONFLICT DO NOTHING)', async () => {
    // F7: `aweme_id` is the global key and a re-run legitimately re-sends the
    // same items. `DO NOTHING` is what keeps the second pass free -- and it is
    // also what stops a second enumerate from writing these rows back to
    // `pending` behind an operator's back.
    await seedJob();
    await laneFor(new OnePageSource(realPage())).runOnce(fresh());

    await seedJob();
    await laneFor(new OnePageSource(realPage())).runOnce(fresh());

    const rows = await allVideos();
    expect(rows).toHaveLength(PAGE_ROWS);
    expect(rows.filter((r) => r.status === 'skipped')).toHaveLength(PAGE_SKIPPED);
  });
});

/* -------------------------------------------------------------------------- */
/* The hole this change does NOT close                                         */
/* -------------------------------------------------------------------------- */

describe('KNOWN GAP -- the cancel-revival sweep brings image posts back', () => {
  it('resets them to pending, because it sweeps by status and there is no other column to read', async () => {
    // THIS TEST ASSERTS A BUG, on purpose, and it is here so the bug is a line
    // of output rather than a paragraph in a comment nobody opens.
    //
    // `reviveSkippedForProfile` exists because phase 6 lost videos permanently:
    // a cancelled job marked them `skipped`, nothing reset `skipped`, and
    // `claimOne` only looks at `pending`. It sweeps by STATUS -- and image posts
    // are now in that status too, so a new job on this profile (or /retry on a
    // cancelled one) hands them back to the download lane, which fetches the
    // background-music MP3 and commits it as `.mp4`. The mapper's guard is not
    // consulted a second time, because enumerate's `DO NOTHING` never revisits
    // an existing row.
    //
    // It is NOT fixable by narrowing the sweep to `last_error IS NULL`: a video
    // that failed once carries a `last_error`, and if it is then cancelled
    // mid-claim `markSkipped` leaves that text in place -- so the predicate
    // would strand a downloadable video in `skipped` forever, which is the same
    // permanent loss from the other side.
    //
    // Closing it needs the reason in a column that means it (a `kind` on
    // `videos` -- a migration), or a refusal at claim time in the download lane.
    // Either is a separate change with its own mutation. WHEN ONE LANDS, THIS
    // TEST MUST GO RED; flip it then.
    await seedJob();
    await laneFor(new OnePageSource(realPage())).runOnce(fresh());

    const revived = await reviveSkippedForProfile(db, PROFILE);

    expect(revived).toBe(PAGE_SKIPPED);
    const claimed: string[] = [];
    for (let i = 0; i < PAGE_ROWS; i += 1) {
      const row = await claimOne(db, CLAIM_OPTS);
      if (row) claimed.push(row.awemeId);
    }
    // All four claimable again -- including the three whose play_addr is audio.
    expect(claimed.sort()).toEqual([VIDEO_ID, ...IMAGE_POST_IDS].sort());
  });
});
