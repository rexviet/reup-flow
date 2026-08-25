import type { Video } from '../db/schema';
import { redactor } from '../redaction/redactor';

/**
 * Public, snake_case shape of a video row. Listed out by hand, not a spread
 * of `Video` -- same reasoning as jobs.mapper.ts.
 *
 * `play_addr` / `url_expires_at` are also deliberately excluded, alongside
 * `lease_expires_at`: they are F1's internal re-resolve cache (a Douyin CDN
 * URL good for a limited window, re-resolved reactively by phase 6), not
 * something an operator browsing a profile's videos needs, and exposing a
 * live signed CDN URL through this API is not a tradeoff worth making for a
 * field nothing here reads.
 */
export interface VideoResponse {
  aweme_id: string;
  sec_user_id: string;
  desc: string | null;
  create_time: string;
  duration_ms: number | null;
  storage_path: string | null;
  status: string;
  attempts: number;
  last_error: string | null;
}

export function toVideoResponse(video: Video): VideoResponse {
  return {
    aweme_id: video.awemeId,
    sec_user_id: video.secUserId,
    desc: video.desc,
    create_time: video.createTime.toISOString(),
    duration_ms: video.durationMs,
    storage_path: video.storagePath,
    status: video.status,
    attempts: video.attempts,
    // Redacted here as well as at the write path (videos.repository.ts's
    // markFailed/reapOrphans) -- write-side redaction keeps the secret out
    // of the database itself (an operator with `psql`, or a future export/
    // backup, never sees it); this read-side pass is what still catches a
    // row written by any path that bypassed the setter -- a migration, a
    // manual `UPDATE`, a future repository function that forgets to redact.
    // Phase 6/7 will start writing attacker-influenced text into this
    // column, which is why both sides land now rather than then.
    last_error: video.lastError === null ? null : redactor.redact(video.lastError),
  };
}
