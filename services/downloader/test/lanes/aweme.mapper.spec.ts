/**
 * Phase 5 -- turning one raw `aweme_list` item into a `videos` row.
 *
 * A file of its own because it is the one place in the lane that TRUSTS
 * Douyin's shape, and every field it reads is a field Douyin can change or
 * omit. `parser.client.ts` deliberately hands items through unreshaped (F1), so
 * this is where a missing key either becomes a null column or drops the item --
 * and getting that boundary wrong is how a lane starts throwing on page 14 of a
 * profile, or worse, starts inventing data.
 *
 * Pure and synchronous: no database, no parser, no clock. `url_expires_at` is
 * not computed here on purpose -- it comes from the DATABASE clock inside
 * `upsertMany` (see the note there).
 */
import { toVideoUpsert } from '../../src/lanes/aweme.mapper';

const PROFILE = 'MS4wLjABAAAA_mapper';

/** A well-formed item, exactly the shape Douyin's `aweme/post` returns. */
function aweme(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    aweme_id: '7300000000000000001',
    desc: 'con voi và những con rùa',
    create_time: 1735689600, // SECONDS -- Douyin's unit, not milliseconds
    video: {
      duration: 15_500,
      play_addr: { url_list: ['https://cdn.example/a.mp4', 'https://cdn.example/b.mp4'] },
    },
    ...overrides,
  };
}

describe('a well-formed item', () => {
  it('maps every field, converting create_time from seconds to a Date', () => {
    const row = toVideoUpsert(aweme(), PROFILE);

    expect(row).toEqual({
      awemeId: '7300000000000000001',
      secUserId: PROFILE,
      desc: 'con voi và những con rùa',
      createTime: new Date(1735689600 * 1000),
      durationMs: 15_500,
      playAddr: 'https://cdn.example/a.mp4',
      // A video is `pending` and carries no reason. `skipped` is for a 图文
      // image post, which has no video in it at all -- decided from `images`,
      // and covered end to end against real payloads in image-post-skip.spec.ts.
      // Asserted here as part of the WHOLE object on purpose: this is the one
      // exhaustive assertion in the file, so a field silently appearing or
      // disappearing shows up as a failure rather than as nothing.
      status: 'pending',
      lastError: null,
    });
  });

  it('takes the FIRST url_list entry', () => {
    // Douyin orders `url_list` best-first, and phase 6 re-resolves reactively
    // on 403/410 rather than trying the alternates. Storing anything but the
    // first would silently pick a worse CDN for every download.
    expect(toVideoUpsert(aweme(), PROFILE)!.playAddr).toBe('https://cdn.example/a.mp4');
  });

  it('accepts create_time as a numeric STRING', () => {
    // Douyin has been seen to send numbers as JSON strings on other fields
    // (`has_more` is why `parser.client.ts` has `toBoolean`). A `new
    // Date("1735689600" * 1000)` style coercion mistake here would produce
    // Invalid Date, which Postgres rejects -- taking the whole page down.
    expect(toVideoUpsert(aweme({ create_time: '1735689600' }), PROFILE)!.createTime).toEqual(
      new Date(1735689600 * 1000),
    );
  });
});

describe('items that must be DROPPED rather than guessed at', () => {
  it('drops an item with no aweme_id', () => {
    // The primary key. There is nothing to insert and nothing to invent.
    expect(toVideoUpsert(aweme({ aweme_id: undefined }), PROFILE)).toBeNull();
  });

  it('drops an item with an empty aweme_id', () => {
    expect(toVideoUpsert(aweme({ aweme_id: '' }), PROFILE)).toBeNull();
  });

  it('drops an item with no readable create_time', () => {
    // `create_time` is NOT NULL in the schema AND it is the download claim
    // order (`videos_pending_claim_idx`). Defaulting it to `now()` would put a
    // 2019 video at the front of the queue and quietly corrupt the archive's
    // ordering; defaulting it to the epoch is the same lie in the other
    // direction. Dropping the item loses one video and says so.
    expect(toVideoUpsert(aweme({ create_time: undefined }), PROFILE)).toBeNull();
    expect(toVideoUpsert(aweme({ create_time: 'not-a-number' }), PROFILE)).toBeNull();
    expect(toVideoUpsert(aweme({ create_time: 0 }), PROFILE)).toBeNull();
  });

  it('drops something that is not an object at all', () => {
    expect(toVideoUpsert(null as never, PROFILE)).toBeNull();
    expect(toVideoUpsert('nope' as never, PROFILE)).toBeNull();
  });
});

describe('fields that become NULL rather than dropping the item', () => {
  it('keeps an item whose play_addr is missing', () => {
    // A video with no cached URL is still a video worth having a row for:
    // phase 6 re-resolves it through `fetch_one_video` on the reactive path,
    // which is the same path an EXPIRED url takes. Dropping the item would
    // silently shrink the archive; nulling the column costs one extra request
    // for that one video.
    const row = toVideoUpsert(aweme({ video: { duration: 1000 } }), PROFILE);
    expect(row).not.toBeNull();
    expect(row!.playAddr).toBeNull();
  });

  it('keeps an item whose url_list is present but empty', () => {
    const row = toVideoUpsert(
      aweme({ video: { play_addr: { url_list: [] } } }),
      PROFILE,
    );
    expect(row!.playAddr).toBeNull();
  });

  it('nulls a duration that is missing or unreadable, rather than storing 0', () => {
    // 0ms is a claim about the video ("it is zero seconds long"); null is the
    // absence of a claim. Phase 10's disk preflight multiplies a per-video
    // estimate rather than reading this, so a wrong 0 here is not caught
    // anywhere downstream.
    expect(toVideoUpsert(aweme({ video: {} }), PROFILE)!.durationMs).toBeNull();
    expect(
      toVideoUpsert(aweme({ video: { duration: 'soon' } }), PROFILE)!.durationMs,
    ).toBeNull();
  });

  it('nulls a missing desc rather than storing an empty string', () => {
    expect(toVideoUpsert(aweme({ desc: undefined }), PROFILE)!.desc).toBeNull();
    expect(toVideoUpsert(aweme({ desc: '' }), PROFILE)!.desc).toBeNull();
  });
});
