import { videoListQuerySchema } from '../../src/videos/videos.schemas';

describe('videoListQuerySchema', () => {
  it('defaults limit to 20, offset to 0, and status to undefined', () => {
    expect(videoListQuerySchema.parse({})).toEqual({ limit: 20, offset: 0 });
  });

  it('coerces query-string numbers and accepts an optional status filter', () => {
    expect(videoListQuerySchema.parse({ limit: '5', offset: '10', status: 'done' })).toEqual({
      limit: 5,
      offset: 10,
      status: 'done',
    });
  });

  it('rejects a limit above 100 or an offset below 0, same bounds as jobs pagination', () => {
    expect(videoListQuerySchema.safeParse({ limit: '101' }).success).toBe(false);
    expect(videoListQuerySchema.safeParse({ offset: '-1' }).success).toBe(false);
  });
});
