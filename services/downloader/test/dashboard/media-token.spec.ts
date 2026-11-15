import {
  MEDIA_KINDS,
  MEDIA_TOKEN_TTL_MS,
  MEDIA_TOKEN_WINDOW_MS,
  signMediaToken,
  verifyMediaToken,
  windowedExpiry,
} from '../../src/dashboard/media-token';

const SECRET = 's'.repeat(40);
const NOW = 1_800_000_000_000;
const claims = { awemeId: '123', generation: 7, kind: 'final' as const, expiresAtMs: NOW + MEDIA_TOKEN_TTL_MS };

describe('media token', () => {
  it('round-trips the signed facts', () => {
    const token = signMediaToken(SECRET, claims);
    expect(verifyMediaToken(SECRET, token, NOW)).toEqual(claims);
  });

  it.each(MEDIA_KINDS)('round-trips a %s token: every kind that can be signed can be verified', (kind) => {
    const given = { ...claims, kind };
    expect(verifyMediaToken(SECRET, signMediaToken(SECRET, given), NOW)).toEqual(given);
  });

  it('rejects an expired token', () => {
    const token = signMediaToken(SECRET, claims);
    expect(verifyMediaToken(SECRET, token, claims.expiresAtMs + 1)).toBeNull();
  });

  it('rejects a token signed with another secret', () => {
    expect(verifyMediaToken('x'.repeat(40), signMediaToken(SECRET, claims), NOW)).toBeNull();
  });

  it('rejects a payload edited after signing', () => {
    const [, sig] = signMediaToken(SECRET, claims).split('.');
    const forged = Buffer.from(`999.7.final.${Math.floor(claims.expiresAtMs / 1000)}`).toString('base64url');
    expect(verifyMediaToken(SECRET, `${forged}.${sig}`, NOW)).toBeNull();
  });

  it.each([undefined, null, 42, '', 'abc', 'a.b.c', 'a'.repeat(600)])('rejects garbage %p', (value) => {
    expect(verifyMediaToken(SECRET, value, NOW)).toBeNull();
  });

  it('a thumb token is verified like the others', () => {
    expect(MEDIA_KINDS).toContain('thumb');
    const given = { ...claims, generation: 0, kind: 'thumb' as const };
    expect(verifyMediaToken(SECRET, signMediaToken(SECRET, given), NOW)).toEqual(given);
  });
});

describe('windowed expiry', () => {
  it('lies at least the TTL ahead, on a window boundary, less than one window past the TTL', () => {
    for (const now of [NOW, NOW + 1, NOW + 123_456, NOW + MEDIA_TOKEN_WINDOW_MS - 1]) {
      const at = windowedExpiry(now);
      expect(at % MEDIA_TOKEN_WINDOW_MS).toBe(0);
      expect(at).toBeGreaterThanOrEqual(now + MEDIA_TOKEN_TTL_MS);
      expect(at).toBeLessThan(now + MEDIA_TOKEN_TTL_MS + MEDIA_TOKEN_WINDOW_MS);
    }
  });

  it('gives the same token, hence the same URL, to every request in one window', () => {
    const sign = (now: number) =>
      signMediaToken(SECRET, { awemeId: '123', generation: 0, kind: 'thumb', expiresAtMs: windowedExpiry(now) });
    const start = Math.ceil(NOW / MEDIA_TOKEN_WINDOW_MS) * MEDIA_TOKEN_WINDOW_MS;
    expect(sign(start + 1)).toBe(sign(start + MEDIA_TOKEN_WINDOW_MS - 1));
    expect(sign(start + 1)).not.toBe(sign(start + MEDIA_TOKEN_WINDOW_MS + 1));
  });
});
