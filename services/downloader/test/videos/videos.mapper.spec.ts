/**
 * Phase 3 (group B) -- explicit camelCase -> snake_case mapper for the
 * browse endpoint, same reasoning as jobs.mapper.spec.ts: an accidental
 * column rename in schema.ts must not silently reshape the public API, and
 * `lease_expires_at` (lane bookkeeping) must never appear in a response.
 */
import type { Video } from '../../src/db/schema';
import { toVideoResponse } from '../../src/videos/videos.mapper';
import { redactor } from '../../src/redaction/redactor';

function fakeVideo(overrides: Partial<Video> = {}): Video {
  return {
    awemeId: 'v1',
    secUserId: 'sec-1',
    desc: 'a caption',
    createTime: new Date('2026-08-01T00:00:00Z'),
    durationMs: 15000,
    playAddr: 'https://cdn.example.test/v1.mp4',
    urlExpiresAt: new Date('2026-08-02T00:00:00Z'),
    storagePath: '/data/sec-1/v1.mp4',
    status: 'done',
    attempts: 1,
    lastError: null,
    leaseExpiresAt: new Date('2026-08-01T01:00:00Z'),
    localizeOnDone: null,
    collectionId: '00000000-0000-4000-8000-000000000001',
    mixEpisode: null,
    collectionManual: false,
    ...overrides,
  };
}

describe('toVideoResponse', () => {
  it('maps operator-facing fields to their snake_case public name', () => {
    const response = toVideoResponse(fakeVideo());

    expect(response).toMatchObject({
      aweme_id: 'v1',
      sec_user_id: 'sec-1',
      desc: 'a caption',
      create_time: '2026-08-01T00:00:00.000Z',
      duration_ms: 15000,
      storage_path: '/data/sec-1/v1.mp4',
      status: 'done',
      attempts: 1,
      last_error: null,
    });
  });

  it('never includes lease_expires_at -- lane bookkeeping, not operator-facing', () => {
    const response = toVideoResponse(fakeVideo());

    expect(response).not.toHaveProperty('lease_expires_at');
    expect(Object.keys(response)).not.toContain('leaseExpiresAt');
  });

  // FIX 3 (review): read-side half of the `last_error` leak fix -- see the
  // write-side test on `markFailed` in videos.repository.spec.ts for why
  // BOTH sides are redacted rather than just one. This mapper is the last
  // chance before `last_error` reaches the browse endpoint's response body.
  it('redacts a cookie-shaped value in `last_error` even if it reached the row unredacted', () => {
    redactor.clear();
    const response = toVideoResponse(
      fakeVideo({ lastError: 'download failed: sessionid=sentinel-fake-video-error-0123456789' }),
    );

    expect(response.last_error).not.toContain('sentinel-fake-video-error-0123456789');
    expect(response.last_error).toContain('sessionid=[redacted]');
  });
});
