import { playAddrUrls, type Aweme } from '../parser/parser.client';
import type { VideoUpsert } from '../db/videos.repository';

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
 * What it must never do is default. A `create_time` defaulted to `now()` puts
 * a 2019 video at the front of the download queue
 * (`videos_pending_claim_idx` orders by it) and nothing downstream would ever
 * report that as wrong -- the same class of mistake as inventing a cause for a
 * 400 (F4).
 *
 * `url_expires_at` is deliberately NOT computed here. It comes from the
 * database clock inside `upsertMany`, like every other lease in this schema.
 */
export function toVideoUpsert(aweme: Aweme, secUserId: string): VideoUpsert | null {
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

  return {
    awemeId,
    secUserId,
    desc: text(aweme.desc),
    createTime: new Date(createTimeSeconds * 1000),
    // Douyin's `video.duration` is already milliseconds.
    durationMs: numeric(isRecord(video) ? video.duration : undefined),
    // First entry only: Douyin orders `url_list` best-first, and phase 6
    // re-resolves reactively on 403/410 rather than walking alternates.
    playAddr: urls[0] ?? null,
  };
}

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
