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
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { toVideoUpsert } from '../../src/parser/aweme.mapper';
import { awemeFixture } from '../support/aweme-fixtures';

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
      // This synthetic item carries neither `mix_info` nor `author`; both are
      // absent claims, never a guessed 合集 or a placeholder channel name.
      mix: null,
      authorNickname: null,
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

/**
 * 合集 (`mix_info`) and channel name (`author.nickname`).
 *
 * The real payloads are `test/fixtures/aweme/user-post-mix-page.json` (one
 * `fetch_user_post_videos` page of channel IM-薛珍珠: ten videos of the 合集
 * 成名的代价 plus one video with no `mix_info`) and `video-detail-mix.json`
 * (`fetch_one_video` for episode 1). Both were captured from the running parser
 * on 2026-11-14 and trimmed to the fields the mapper reads -- envelope, item
 * keys and nesting unchanged, every URL reduced to scheme and host so no CDN
 * signature or token is committed.
 */
describe('合集 and channel name', () => {
  const FIXTURE_DIR = join(__dirname, '..', 'fixtures', 'aweme');
  const CHANNEL = 'MS4wLjABAAAAE_UjwhIrz5ADIJh_CmMo31N9BR7pfKz_NKzLf9djdAF-Amx02tIyIo221_zzPIjb';
  const MIX_ID = '7695338683825653823';
  const NO_MIX_ID = '7696076126451518735';

  function pageItems(): Record<string, unknown>[] {
    const envelope = JSON.parse(
      readFileSync(join(FIXTURE_DIR, 'user-post-mix-page.json'), 'utf8'),
    ) as { data?: { aweme_list?: unknown } };
    const list = envelope.data?.aweme_list;
    if (!Array.isArray(list) || list.length === 0) {
      throw new Error('user-post-mix-page.json has no .data.aweme_list -- every assertion would be vacuous');
    }
    return list as Record<string, unknown>[];
  }

  function withMix(mixInfo: unknown): Record<string, unknown> {
    return aweme({ mix_info: mixInfo });
  }

  const validMix = (overrides: Record<string, unknown> = {}) => ({
    mix_id: MIX_ID,
    mix_name: '成名的代价',
    statis: { current_episode: 3, updated_to_episode: 10 },
    ...overrides,
  });

  describe('real enumerate page', () => {
    it('reads the 合集 and episode of all ten episodes', () => {
      const rows = pageItems().map((item) => toVideoUpsert(item, CHANNEL)!);
      const inMix = rows.filter((row) => row.mix !== null);

      expect(rows).toHaveLength(11);
      expect(inMix).toHaveLength(10);
      for (const row of inMix) {
        expect(row.mix!.mixId).toBe(MIX_ID);
        expect(row.mix!.mixName).toBe('成名的代价');
      }
      expect(inMix.map((row) => row.mix!.episode).sort((a, b) => a! - b!)).toEqual([
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
      ]);
    });

    it('gives the item with no mix_info `mix: null`, and keeps it', () => {
      const item = pageItems().find((i) => i.aweme_id === NO_MIX_ID)!;
      const row = toVideoUpsert(item, CHANNEL);

      expect(row).not.toBeNull();
      expect(row!.mix).toBeNull();
      expect(row!.status).toBe('pending');
    });

    it('reads the channel name from every item', () => {
      for (const item of pageItems()) {
        expect(toVideoUpsert(item, CHANNEL)!.authorNickname).toBe('IM-薛珍珠');
      }
    });
  });

  describe('real video detail', () => {
    it('reads episode 1 of the 合集 and the channel name', () => {
      const row = toVideoUpsert(awemeFixture('video-detail-mix.json'), CHANNEL)!;

      expect(row.awemeId).toBe('7689288513854595771');
      expect(row.mix).toEqual({ mixId: MIX_ID, mixName: '成名的代价', episode: 1 });
      expect(row.authorNickname).toBe('IM-薛珍珠');
    });
  });

  describe('mix_info that cannot honestly name a 合集 becomes `mix: null`', () => {
    it.each([
      ['an empty mix_id', validMix({ mix_id: '' })],
      ['a numeric mix_id (precision is already lost in JSON)', validMix({ mix_id: Number(MIX_ID) })],
      ['a non-digit mix_id', validMix({ mix_id: 'abc123' })],
      ['a zero mix_id (Douyin uses "0" for "none")', validMix({ mix_id: '0' })],
      ['a zero-padded mix_id', validMix({ mix_id: '0123' })],
      ['a missing mix_id', validMix({ mix_id: undefined })],
      ['mix_info that is not an object', 'collection'],
      ['mix_info that is null', null],
      ['mix_info that is an array', [validMix()]],
    ])('%s', (_label, mixInfo) => {
      const row = toVideoUpsert(withMix(mixInfo), PROFILE);
      expect(row).not.toBeNull();
      expect(row!.mix).toBeNull();
    });
  });

  describe('episode is a positive integer or null', () => {
    it.each([
      ['0', 0],
      ['negative', -1],
      ['fractional', 2.5],
      ['non-numeric text', 'first'],
      ['missing', undefined],
      ['beyond a Postgres integer', 2_147_483_648],
      ['astronomically large', 1e300],
      ['hex text', '0x10'],
      ['exponent text', '1e1'],
    ])('%s current_episode -> episode null, 合集 kept', (_label, episode) => {
      const row = toVideoUpsert(withMix(validMix({ statis: { current_episode: episode } })), PROFILE)!;
      expect(row.mix).toEqual({ mixId: MIX_ID, mixName: '成名的代价', episode: null });
    });

    it('keeps the 合集 when statis itself is missing', () => {
      const row = toVideoUpsert(withMix(validMix({ statis: undefined })), PROFILE)!;
      expect(row.mix).toEqual({ mixId: MIX_ID, mixName: '成名的代价', episode: null });
    });

    it('accepts the largest Postgres integer', () => {
      const row = toVideoUpsert(withMix(validMix({ statis: { current_episode: 2_147_483_647 } })), PROFILE)!;
      expect(row.mix!.episode).toBe(2_147_483_647);
    });

    it('accepts a numeric-string episode, as Douyin sends numbers as strings elsewhere', () => {
      const row = toVideoUpsert(withMix(validMix({ statis: { current_episode: '7' } })), PROFILE)!;
      expect(row.mix!.episode).toBe(7);
    });
  });

  describe('mix_name is cleaned, and falls back to the mix_id', () => {
    it('cuts a 500-character name to 200 code points', () => {
      const row = toVideoUpsert(withMix(validMix({ mix_name: '代'.repeat(500) })), PROFILE)!;
      expect(Array.from(row.mix!.mixName)).toHaveLength(200);
    });

    it('uses the mix_id when the name is only zero-width characters', () => {
      const row = toVideoUpsert(withMix(validMix({ mix_name: '​‍﻿' })), PROFILE)!;
      expect(row.mix!.mixName).toBe(MIX_ID);
    });

    it('uses the mix_id when the name is missing', () => {
      const row = toVideoUpsert(withMix(validMix({ mix_name: undefined })), PROFILE)!;
      expect(row.mix!.mixName).toBe(MIX_ID);
    });
  });

  describe('author nickname', () => {
    it('is null when Douyin sent no author or no nickname', () => {
      expect(toVideoUpsert(aweme({ author: undefined }), PROFILE)!.authorNickname).toBeNull();
      expect(toVideoUpsert(aweme({ author: {} }), PROFILE)!.authorNickname).toBeNull();
      expect(toVideoUpsert(aweme({ author: { nickname: '' } }), PROFILE)!.authorNickname).toBeNull();
      expect(toVideoUpsert(aweme({ author: { nickname: 42 } }), PROFILE)!.authorNickname).toBeNull();
    });

    it('is cleaned the same way as a 合集 name', () => {
      const row = toVideoUpsert(aweme({ author: { nickname: ' IM-‮薛珍珠\n ' } }), PROFILE)!;
      expect(row.authorNickname).toBe('IM-薛珍珠');
    });

    it('is null when the item names a different author than the channel being read', () => {
      // A co-created or reposted item on this channel's list must not hand its
      // own author's name to this channel.
      const row = toVideoUpsert(
        aweme({ author: { sec_uid: 'MS4wLjABAAAA_someone_else', nickname: 'Someone Else' } }),
        PROFILE,
      )!;
      expect(row.authorNickname).toBeNull();
    });

    it('is read when the item names this channel as its author', () => {
      const row = toVideoUpsert(aweme({ author: { sec_uid: PROFILE, nickname: 'IM-薛珍珠' } }), PROFILE)!;
      expect(row.authorNickname).toBe('IM-薛珍珠');
    });

    it('falls back to the sec_user_id when nothing is left after cleaning', () => {
      const row = toVideoUpsert(aweme({ author: { nickname: '​​' } }), PROFILE)!;
      expect(row.authorNickname).toBe(PROFILE);
    });
  });
});
