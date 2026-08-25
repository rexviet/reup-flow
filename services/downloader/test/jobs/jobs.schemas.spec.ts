/**
 * Phase 3 (group B) -- the zod schemas jobs.controller.ts validates every
 * request against. `paginationSchema`'s bounds are the DoD's exact numbers:
 * limit default 20 / max 100, offset default 0 / min 0.
 */
import { createJobSchema, paginationSchema, uuidParamSchema } from '../../src/jobs/jobs.schemas';

describe('createJobSchema', () => {
  it('accepts a non-empty url', () => {
    expect(createJobSchema.safeParse({ url: 'https://www.douyin.com/user/x' }).success).toBe(true);
  });

  it('rejects a missing or empty url', () => {
    expect(createJobSchema.safeParse({}).success).toBe(false);
    expect(createJobSchema.safeParse({ url: '' }).success).toBe(false);
  });

  // FIX 10 (review): `resolveSecUserId(url)` hands this value verbatim to
  // the parser, which makes the parser container FOLLOW it -- and the
  // parser deliberately has no `ports:` in docker-compose.yml precisely
  // because it is an unauthenticated scraper with no auth of its own. A
  // schema that accepted `z.string().min(1)` handed an authenticated
  // caller of THIS API a way to make the parser fetch an arbitrary internal
  // address (SSRF), one hop removed from this service's own auth.
  describe('SSRF guard: url must be an http(s) Douyin URL', () => {
    it.each([
      ['a short link', 'https://v.douyin.com/aBcDeFg/'],
      ['a full profile URL', 'https://www.douyin.com/user/MS4wLjABAAAA_sec_user_id'],
      ['a www subdomain of the CDN-ish domain', 'https://www.iesdouyin.com/share/user/xyz'],
      ['a bare apex domain', 'https://douyin.com/user/x'],
      ['a bare apex of the second allowed domain', 'https://iesdouyin.com/share/user/xyz'],
    ])('accepts %s', (_label, url) => {
      expect(createJobSchema.safeParse({ url }).success).toBe(true);
    });

    it.each([
      ['a cloud metadata address', 'http://169.254.169.254/latest/meta-data/'],
      ['a plain internal IP', 'http://10.0.0.5/admin'],
      ['localhost', 'http://localhost:6379/'],
      ['an unrelated domain', 'https://evil.example.com/user/x'],
      // Not merely "contains douyin.com" -- a suffix match without the dot
      // boundary would accept `notdouyin.com` or `douyin.com.evil.com`,
      // both of which are NOT the real domain.
      ['a lookalike domain missing the dot boundary', 'https://notdouyin.com/user/x'],
      ['a lookalike domain with douyin.com as a prefix of a longer host', 'https://douyin.com.evil.com/user/x'],
      ['a non-http(s) scheme', 'ftp://douyin.com/user/x'],
      ['a malformed URL', 'not a url'],
    ])('rejects %s', (_label, url) => {
      expect(createJobSchema.safeParse({ url }).success).toBe(false);
    });
  });
});

describe('paginationSchema', () => {
  it('defaults limit to 20 and offset to 0', () => {
    const result = paginationSchema.parse({});
    expect(result).toEqual({ limit: 20, offset: 0 });
  });

  it('coerces query-string numbers', () => {
    expect(paginationSchema.parse({ limit: '5', offset: '10' })).toEqual({ limit: 5, offset: 10 });
  });

  it('rejects a limit above 100', () => {
    expect(paginationSchema.safeParse({ limit: '101' }).success).toBe(false);
  });

  it('rejects an offset below 0', () => {
    expect(paginationSchema.safeParse({ offset: '-1' }).success).toBe(false);
  });

  it('rejects a limit below 1', () => {
    expect(paginationSchema.safeParse({ limit: '0' }).success).toBe(false);
  });
});

describe('uuidParamSchema', () => {
  it('accepts a valid UUID', () => {
    expect(uuidParamSchema.safeParse('550e8400-e29b-41d4-a716-446655440000').success).toBe(true);
  });

  it('rejects a non-UUID -- a short link slug, for instance -- rather than letting it reach Postgres as 22P02', () => {
    expect(uuidParamSchema.safeParse('not-a-uuid').success).toBe(false);
  });
});
