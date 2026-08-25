/**
 * Phase 3 (group B) -- the explicit mapper from Drizzle's camelCase `Job`
 * row to the snake_case public API shape. Explicit on purpose: returning the
 * ORM row directly would mean an accidental column rename in schema.ts
 * silently reshapes the public API, and there would be nothing forcing
 * `lease_expires_at` (lane bookkeeping, never operator-facing) to stay out
 * of the response.
 */
import type { Job } from '../../src/db/schema';
import { toJobDetailResponse, toJobResponse } from '../../src/jobs/jobs.mapper';
import { redactor } from '../../src/redaction/redactor';

function fakeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    secUserId: 'sec-1',
    status: 'pending',
    totalVideos: null,
    enumeratedCount: 0,
    downloadedCount: 0,
    failedCount: 0,
    maxCursor: 0,
    leaseExpiresAt: new Date('2026-08-25T00:00:00Z'),
    error: null,
    createdAt: new Date('2026-08-24T00:00:00Z'),
    updatedAt: new Date('2026-08-24T00:00:00Z'),
    ...overrides,
  };
}

describe('toJobResponse', () => {
  it('maps every field to its snake_case public name', () => {
    const response = toJobResponse(
      fakeJob({ totalVideos: 10, enumeratedCount: 5, downloadedCount: 3, failedCount: 1, maxCursor: 999 }),
    );

    expect(response).toEqual({
      job_id: 'job-1',
      sec_user_id: 'sec-1',
      status: 'pending',
      total_videos: 10,
      enumerated_count: 5,
      downloaded_count: 3,
      failed_count: 1,
      max_cursor: 999,
      error: null,
      created_at: '2026-08-24T00:00:00.000Z',
      updated_at: '2026-08-24T00:00:00.000Z',
    });
  });

  it('never includes lease_expires_at -- lane bookkeeping, not operator-facing', () => {
    const response = toJobResponse(fakeJob());

    expect(response).not.toHaveProperty('lease_expires_at');
    expect(Object.keys(response)).not.toContain('leaseExpiresAt');
  });

  // FIX 3 (review): phase-3's risk register names this leak explicitly --
  // "cookie vào error của job rồi ra API response" -- and it was
  // unimplemented. This is the READ-side half of the fix (see the write-side
  // test on markFailed in videos.repository.spec.ts): a row that got a
  // cookie into `error` through ANY path -- a future setter that forgets to
  // redact, a manual `UPDATE`, a migration backfill -- must still come out
  // scrubbed here, because this mapper is the last chance before the value
  // reaches an API response.
  it('redacts a cookie-shaped value in `error` even if it reached the row unredacted', () => {
    // Keeps this assertion independent of suite run order: the redactor
    // singleton is process-wide, and the pattern mechanism this exercises
    // needs no registration anyway, but clearing rules out a stray
    // registered secret from another suite explaining a pass.
    redactor.clear();
    const response = toJobResponse(
      fakeJob({ error: 'parser call failed: sessionid=sentinel-fake-job-error-0123456789' }),
    );

    expect(response.error).not.toContain('sentinel-fake-job-error-0123456789');
    expect(response.error).toContain('sessionid=[redacted]');
  });
});

describe('toJobDetailResponse', () => {
  it('includes profile_video_counts (F7) alongside the job fields', () => {
    const response = toJobDetailResponse(fakeJob(), { done: 3, pending: 1 });

    expect(response.profile_video_counts).toEqual({ done: 3, pending: 1 });
    expect(response.job_id).toBe('job-1');
    expect(response).not.toHaveProperty('lease_expires_at');
  });
});
