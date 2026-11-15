import { playAddrUrls, type Aweme } from './parser.client';
import type { VideoUpsert } from '../db/videos.repository';
import { cleanDisplayName } from './display-name';

/** The Douyin 合集 (collection) an item belongs to, read from its `mix_info`. */
export interface AwemeMix {
  /** Douyin's id, a digit string without leading zero. Kept as text: it exceeds 2^53. */
  mixId: string;
  /** Cleaned for display (`cleanDisplayName`); the `mixId` when Douyin sent none. */
  mixName: string;
  /** 1-based position in the 合集 (fits a Postgres integer), or null when Douyin sent no usable one. */
  episode: number | null;
}

/**
 * A `videos` row plus what the item says about where it belongs. The two extra
 * fields are not columns of `videos`; `upsertMany` writes only the columns it
 * knows, so they ride along until collection assignment reads them.
 */
export interface MappedAweme extends VideoUpsert {
  /** Null when the item carries no usable `mix_info` -- never a guessed 合集. */
  mix: AwemeMix | null;
  /**
   * The channel name from `author.nickname`, cleaned for display. Null when
   * Douyin sent none or the item's author is a different account; the
   * profile's `sec_user_id` when the name cleans to nothing.
   */
  authorNickname: string | null;
}

/**
 * One `aweme_list` item -> one `videos` row, or `null` if the item cannot
 * honestly become one.
 *
 * This is the ONE place in the lane that trusts Douyin's shape, and it is a
 * separate file for that reason. `parser.client.ts` hands items through
 * unreshaped on purpose (F1 -- each one already carries the `play_addr` that
 * keeps a 500-video profile at ~25 requests instead of ~525), so every field
 * Douyin can rename, retype, or omit arrives here.
 *
 * The whole design of this function is one decision made field by field:
 *
 *   DROP THE ITEM when the missing field is one we cannot invent -- the
 *   primary key, or the sort key the download queue is ordered by. Losing one
 *   video is visible in `total_videos` and recoverable with a re-run.
 *
 *   NULL THE COLUMN when the missing field only costs work later. A video with
 *   no cached URL is still worth a row: phase 6's reactive re-resolve already
 *   handles exactly that case for EXPIRED urls, so an absent one costs one
 *   extra request for one video.
 *
 *   MARK IT `skipped` when the item is a real post with nothing downloadable in
 *   it -- a 图文 image post (`isImagePost`). NOT dropped: dropping shrinks
 *   `total_videos` and the post disappears with every counter still agreeing,
 *   which is the exact failure that guard exists to end. `skipped` keeps it
 *   visible, counted, and out of the download queue.
 *
 * What it must never do is default. A `create_time` defaulted to `now()` puts
 * a 2019 video at the front of the download queue
 * (`videos_pending_claim_idx` orders by it) and nothing downstream would ever
 * report that as wrong -- the same class of mistake as inventing a cause for a
 * 400 (F4).
 *
 * `url_expires_at` is deliberately NOT computed here. It comes from the
 * database clock inside `upsertMany`, like every other lease in this schema.
 */
export function toVideoUpsert(aweme: Aweme, secUserId: string): MappedAweme | null {
  if (typeof aweme !== 'object' || aweme === null) return null;

  const awemeId = text(aweme.aweme_id);
  if (awemeId === null) return null;

  // Douyin sends `create_time` in SECONDS. Multiplying is the whole conversion,
  // and getting the unit wrong lands every video in 1970 -- which is a valid
  // timestamp, so nothing rejects it.
  const createTimeSeconds = numeric(aweme.create_time);
  if (createTimeSeconds === null || createTimeSeconds <= 0) return null;

  const video = aweme.video;
  const urls = playAddrUrls(aweme);
  const imagePost = isImagePost(aweme);

  return {
    awemeId,
    secUserId,
    desc: text(aweme.desc),
    createTime: new Date(createTimeSeconds * 1000),
    // Douyin's `video.duration` is already milliseconds.
    durationMs: numeric(isRecord(video) ? video.duration : undefined),
    // First entry only: Douyin orders `url_list` best-first, and phase 6
    // re-resolves reactively on 403/410 rather than walking alternates.
    //
    // KEPT AS-IS for an image post, MP3 and all. It is the evidence: an
    // operator reading a `skipped` row sees the `.mp3` that would have been
    // downloaded, which is a more useful answer than a null column. Nothing
    // reads it while the row is `skipped` -- `claimOne` only looks at `pending`.
    playAddr: urls[0] ?? null,
    status: imagePost ? 'skipped' : 'pending',
    lastError: imagePost ? IMAGE_POST_REASON : null,
    mix: readMix(aweme.mix_info),
    authorNickname: readNickname(aweme.author, secUserId),
  };
}

/**
 * `mix_info` -> the 合集, or null. Present on both `aweme/post` and
 * `aweme/detail` items (checked 2026-11-14), so no extra request is needed.
 *
 * Same rule as the rest of this file: a field that cannot honestly name a 合集
 * yields null and the video falls back to its channel's collection, rather than
 * being grouped under a guess. `mix_id` must be a digit STRING -- a JSON number
 * of that size has already lost precision, so it is refused, not stringified.
 * A missing or non-positive episode keeps the 合集 and nulls only the episode.
 */
function readMix(mixInfo: unknown): AwemeMix | null {
  if (!isRecord(mixInfo)) return null;

  const mixId = mixInfo.mix_id;
  if (typeof mixId !== 'string' || !MIX_ID.test(mixId)) return null;

  const statis = mixInfo.statis;
  return {
    mixId,
    mixName: cleanDisplayName(mixInfo.mix_name, mixId),
    episode: readEpisode(isRecord(statis) ? statis.current_episode : undefined),
  };
}

/**
 * A digit string with no leading zero. `"0"` is refused: Douyin uses zero ids
 * to mean "none", and no real 合集 has id 0.
 */
const MIX_ID = /^[1-9]\d*$/;

/** The largest value a Postgres `integer` column holds. */
const PG_INTEGER_MAX = 2_147_483_647;

/**
 * A positive integer that fits a Postgres `integer`, or null.
 *
 * A number or a plain decimal digit string only. `numeric()` is not reused
 * here because `Number()` reads `'0x10'` as 16 and `'1e1'` as 10, which are
 * not episode numbers anyone sent. The upper bound matters because rows are
 * inserted page by page: one out-of-range episode would fail the whole
 * multi-row insert, losing the page, not just the video.
 */
function readEpisode(value: unknown): number | null {
  let n: number;
  if (typeof value === 'number') n = value;
  else if (typeof value === 'string' && /^\d+$/.test(value)) n = Number(value);
  else return null;
  return Number.isSafeInteger(n) && n > 0 && n <= PG_INTEGER_MAX ? n : null;
}

/**
 * `author.nickname`, cleaned. Null when Douyin sent no name at all (absence is
 * not invented into a placeholder); `secUserId` when a name was sent but is
 * nothing but invisible characters, since it still names this channel.
 *
 * Also null when the item's `author.sec_uid` names someone OTHER than the
 * channel being read: a co-created or reposted item on this channel's list
 * would otherwise give this channel another person's name. An item with no
 * `sec_uid` cannot be checked and is read as this channel's own post, which is
 * what being on its post list means.
 */
function readNickname(author: unknown, secUserId: string): string | null {
  if (!isRecord(author)) return null;
  const authorSecUid = text(author.sec_uid);
  if (authorSecUid !== null && authorSecUid !== secUserId) return null;
  const raw = text(author.nickname);
  return raw === null ? null : cleanDisplayName(raw, secUserId);
}

/**
 * Is this a 图文 (image / photo-slideshow) post rather than a video?
 *
 * THE BUG THIS EXISTS FOR, measured on a real 215-item profile: an image post
 * has no video, and its `video.play_addr.url_list` points at the BACKGROUND
 * MUSIC CDN. The download lane fetched that, got an MP3, wrote it to
 * `<aweme_id>.mp4`, verified the length, renamed it into place and marked the
 * row `done`. The run reported 215/215 completed, 0 zero-byte files, 0 leftover
 * `.tmp` -- and 2 of the 215 files were `MPEG ADTS, layer III, 128 kbps`. Every
 * counter agreed. The image content was never saved at all. Same family as
 * phase 6's zero-byte commit: the archive was wrong while every number was
 * right.
 *
 * THE DISCRIMINATOR IS `images`, and specifically a NON-EMPTY ARRAY, because it
 * is the only DIRECT evidence in the payload. The field IS the image content --
 * five entries, each with `url_list`, `width`, `height`, `uri` -- so its
 * presence is the post saying what it is, not us inferring it. The three
 * alternatives were each rejected for a reason:
 *
 *   `media_type` / `aweme_type` (2/68 for images, 4/55 for video) are ENUMS on
 *   somebody else's API. They are a closed set today; Douyin adds values
 *   (live replays, collections, mixed posts). An allowlist of video types
 *   silently skips a whole new kind of real video, and a denylist of image
 *   types silently downloads the next MP3 -- and both failures look exactly
 *   like this bug looked: correct-seeming counters.
 *
 *   THE CDN HOSTNAME (`*-music*.douyinstatic.com` vs `douyinvod.com`) is
 *   infrastructure. It is not part of the contract, it is regionalised
 *   (`lf26-music-east`, `sf6-cdn-tos`), and a hostname allowlist would break the
 *   first time Douyin moves a bucket -- in the direction of skipping real
 *   videos.
 *
 *   `duration === 0` is a CONSEQUENCE, not the fact. It is also true of a real
 *   video whose `video` object arrived malformed, and `toVideoUpsert` already
 *   refuses to read absence as a number for exactly that reason.
 *
 * An EMPTY array is deliberately not enough. `images: []` is Douyin saying
 * "there are none", which is what a video says; treating it as evidence would
 * skip real videos, and a skipped video is the same silent loss in the other
 * direction. `images: null` (what the video fixture actually carries) is the
 * same case.
 *
 * WHAT THIS DOES NOT PROVE, written down rather than assumed away: if Douyin
 * renames `images`, image posts fall back to today's behaviour -- an MP3 saved
 * as `.mp4` and marked `done`. This guard makes the KNOWN shape safe; it is not
 * a proof that no MP3 can ever be committed. The only place that could prove
 * that is the download lane, by checking what came back off the wire before it
 * commits (no such check exists today, and adding one is a separate change with
 * its own mutation).
 */
function isImagePost(aweme: Record<string, unknown>): boolean {
  const images = aweme.images;
  return Array.isArray(images) && images.length > 0;
}

/**
 * Written to `videos.last_error`, and the reason THIS skip carries one while
 * `markSkipped`'s does not.
 *
 * `markSkipped` (a cancelled job) leaves `last_error` null on purpose: nothing
 * went wrong, so there is nothing to say. Here something IS worth saying,
 * because the two skips answer different operator questions and share one
 * status. "Nobody wanted this video" resolves itself -- `/retry` or a new job
 * brings the row back and it downloads. "This post has no video in it" never
 * resolves: the row will read `skipped` forever, no file will ever appear, and
 * an operator comparing 213 files against 215 items needs the row itself to
 * explain the difference. A null `last_error` would leave that as a subtraction
 * to be worked out by hand, which is how the 2 MP3s went unnoticed in the first
 * place.
 *
 * NOT a discriminator for code to branch on, and specifically not the one
 * `reviveSkippedForProfile` could use: a video that failed once (`last_error`
 * set) and was then cancelled mid-claim is `skipped` WITH a `last_error` too, so
 * "has a reason" does not mean "is an image post". See the note on
 * `reviveSkippedForProfile` for the consequence that leaves open.
 */
const IMAGE_POST_REASON =
  'image post (图文): Douyin ships no video for this item and video.play_addr points at the ' +
  'background music CDN, so downloading it would store an MP3 as .mp4';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/** A non-empty string, or null. An empty string is an absent value, not a value. */
function text(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

/**
 * A finite number, or null -- never a silent 0.
 *
 * Accepts a numeric string as well, because Douyin does send numbers as JSON
 * strings (`has_more` is why `parser.client.ts` owns a `toBoolean`), and
 * `Number(undefined)` is `NaN` while `Number(null)` and `Number('')` are both
 * `0` -- so a bare `Number()` here would turn two different kinds of absence
 * into a confident zero.
 */
function numeric(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string' && value.trim() !== '') {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}
