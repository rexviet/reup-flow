import type { Job } from '../db/schema';
import { redactor } from '../redaction/redactor';

/**
 * Public, snake_case shape of a job. Written out explicitly rather than
 * derived from `Job` -- an accidental column rename in schema.ts must fail
 * this file's compile (or this test's assertion), not silently reshape the
 * API a caller depends on.
 */
export interface JobResponse {
  job_id: string;
  sec_user_id: string;
  status: string;
  total_videos: number | null;
  enumerated_count: number;
  downloaded_count: number;
  failed_count: number;
  max_cursor: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobDetailResponse extends JobResponse {
  /** F7 -- profile-wide video status counts, not a per-job delta. See
   * countByStatusForProfile in videos.repository.ts for why. */
  profile_video_counts: Record<string, number>;
}

/**
 * `Job` -> `JobResponse`.
 *
 * Deliberately does NOT spread `...job`: `lease_expires_at` is lane
 * bookkeeping (when the enumerate lane's claim on this job expires), never
 * operator-facing, and a spread would let it leak into every response the
 * moment someone adds a field to the table without also touching this file.
 * Listing every field by hand is what makes leaving one out a decision
 * rather than an oversight.
 */
export function toJobResponse(job: Job): JobResponse {
  return {
    job_id: job.id,
    sec_user_id: job.secUserId,
    status: job.status,
    total_videos: job.totalVideos,
    enumerated_count: job.enumeratedCount,
    downloaded_count: job.downloadedCount,
    failed_count: job.failedCount,
    max_cursor: job.maxCursor,
    // Redacted here too, not only at the write path that sets `error`
    // (there is none yet in phase 3 -- see videos.repository.ts's markFailed
    // for the equivalent that exists) -- phase-3's risk register names this
    // leak explicitly ("cookie vào error của job rồi ra API response"), and
    // this mapper is the last chance before it reaches an API client. A row
    // that acquired a cookie in `error` through a path that bypassed
    // whatever setter exists (a migration, a manual `UPDATE`, a future
    // repository function that forgets to redact) still comes out scrubbed
    // here. `redactor.redact` is a no-op on null-free text with nothing to
    // scrub, so this costs nothing on the common case.
    error: job.error === null ? null : redactor.redact(job.error),
    created_at: job.createdAt.toISOString(),
    updated_at: job.updatedAt.toISOString(),
  };
}

export function toJobDetailResponse(
  job: Job,
  profileVideoCounts: Record<string, number>,
): JobDetailResponse {
  return { ...toJobResponse(job), profile_video_counts: profileVideoCounts };
}
