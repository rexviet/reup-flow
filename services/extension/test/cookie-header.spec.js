'use strict';

const { buildCookieHeader } = require('../lib/cookie-header');

/**
 * Fixture models the REAL shape `chrome.cookies.getAll({ domain:
 * '.douyin.com' })` returns -- not a simplified version of it. Real Douyin
 * session cookies:
 *   - are split across multiple domains (`.douyin.com`, `www.douyin.com`)
 *   - include HttpOnly cookies (`sessionid_ss`, `passport_csrf_token`) that
 *     `document.cookie` cannot see -- this is the entire reason the
 *     extension exists (phase-09 spec §9.1)
 *   - include non-HttpOnly cookies too (`odin_tt`)
 */
const REALISTIC_LOGGED_IN_COOKIES = [
  {
    domain: '.douyin.com',
    name: 'sessionid_ss',
    value: 'a1b2c3d4e5f6g7h8i9j0',
    httpOnly: true,
    secure: true,
    session: false,
    hostOnly: false,
    path: '/',
    sameSite: 'no_restriction',
    storeId: '0',
    expirationDate: 1893456000,
  },
  {
    domain: '.douyin.com',
    name: 'passport_csrf_token',
    value: 'z9y8x7w6v5u4t3s2r1q0',
    httpOnly: true,
    secure: true,
    session: false,
    hostOnly: false,
    path: '/',
    sameSite: 'no_restriction',
    storeId: '0',
    expirationDate: 1893456000,
  },
  {
    domain: 'www.douyin.com',
    name: 'odin_tt',
    value: 'deadbeefcafef00d',
    httpOnly: false,
    secure: true,
    session: false,
    hostOnly: true,
    path: '/',
    sameSite: 'unspecified',
    storeId: '0',
    expirationDate: 1893456000,
  },
];

describe('buildCookieHeader', () => {
  test('joins name=value pairs from chrome.cookies.getAll shape into a Cookie header', () => {
    const header = buildCookieHeader(REALISTIC_LOGGED_IN_COOKIES);
    expect(header).toBe(
      'sessionid_ss=a1b2c3d4e5f6g7h8i9j0; passport_csrf_token=z9y8x7w6v5u4t3s2r1q0; odin_tt=deadbeefcafef00d',
    );
  });

  test('the built header contains sessionid_ss -- proof HttpOnly cookies survive (phase-09 §9.1 reason to exist)', () => {
    const header = buildCookieHeader(REALISTIC_LOGGED_IN_COOKIES);
    expect(header).toContain('sessionid_ss=a1b2c3d4e5f6g7h8i9j0');
    expect(header).toContain('passport_csrf_token=z9y8x7w6v5u4t3s2r1q0');
  });

  test('returns null for an empty array (logged-out shape) -- caller must treat null as do-not-push', () => {
    expect(buildCookieHeader([])).toBeNull();
  });

  test('returns null for non-array input', () => {
    expect(buildCookieHeader(undefined)).toBeNull();
    expect(buildCookieHeader(null)).toBeNull();
  });
});
