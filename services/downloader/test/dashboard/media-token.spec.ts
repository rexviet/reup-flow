import { signMediaToken, verifyMediaToken, MEDIA_TOKEN_TTL_MS } from '../../src/dashboard/media-token';

const SECRET = 's'.repeat(40);
const NOW = 1_800_000_000_000;
const claims = { awemeId: '123', generation: 7, kind: 'final' as const, expiresAtMs: NOW + MEDIA_TOKEN_TTL_MS };

describe('media token', () => {
  it('round-trips the signed facts', () => {
    const token = signMediaToken(SECRET, claims);
    expect(verifyMediaToken(SECRET, token, NOW)).toEqual(claims);
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
});
