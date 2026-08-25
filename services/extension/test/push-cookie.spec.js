'use strict';

// Load dependency libs first so their UMD pattern attaches to globalThis --
// push-cookie.js reads `root.buildCookieHeader` / `root.pushCookie` /
// `root.getSettings` / `root.recordLastPushResult` at call time, not at
// require time, so load order between these four requires doesn't matter as
// long as all four have run before performPush() is invoked.
require('../lib/cookie-header');
require('../lib/api-client');
require('../lib/storage');
const { performPush, DOUYIN_COOKIE_DOMAIN, PLATFORM } = require('../lib/push-cookie');

/** In-memory chrome.storage.local stand-in, same callback shape as real chrome.storage. */
function fakeStorageArea(initial) {
  const data = { ...initial };
  return {
    get(keys, cb) {
      const out = {};
      for (const k of keys) out[k] = data[k];
      cb(out);
    },
    set(items, cb) {
      Object.assign(data, items);
      cb();
    },
  };
}

/** chrome.cookies + chrome.runtime stand-in. */
function fakeChromeApi(cookiesToReturn) {
  return {
    cookies: {
      getAll(details, cb) {
        expect(details.domain).toBe(DOUYIN_COOKIE_DOMAIN);
        cb(cookiesToReturn);
      },
    },
    runtime: {},
  };
}

/**
 * A tiny fake backend that mirrors the REAL invariant under test:
 * `CookieService.push` in services/downloader (the actual endpoint this
 * calls) simply overwrites whatever is in Redis with whatever the request
 * body carries. Nothing on the server side protects against an empty write
 * -- the ONLY thing standing between "logged out" and "good cookie silently
 * erased" is the extension's own guard before it ever sends the request.
 * This fake makes that observable: it tracks `currentCookie` as an outcome,
 * not just whether `fetch` was called.
 */
function fakeBackend() {
  const state = { currentCookie: null, pushCount: 0 };
  const fetchImpl = async (url, init) => {
    if (url.endsWith('/internal/cookie') && init.method === 'POST') {
      state.pushCount += 1;
      const body = JSON.parse(init.body);
      state.currentCookie = body.cookie; // unconditional overwrite, exactly like the real service
      return { ok: true, status: 200, json: async () => ({ ok: true }), text: async () => '' };
    }
    throw new Error(`fakeBackend: unexpected request ${init.method} ${url}`);
  };
  return { state, fetchImpl };
}

const LOGGED_IN_COOKIES = [
  { name: 'sessionid_ss', value: 'good-session-value-1' },
  { name: 'passport_csrf_token', value: 'good-csrf-value-1' },
];

describe('performPush', () => {
  test('pushes the built Cookie header to the real backend on success and records ok=true', async () => {
    const storageArea = fakeStorageArea({ backendUrl: 'http://backend', apiKey: 'k' });
    const chromeApi = fakeChromeApi(LOGGED_IN_COOKIES);
    const { state, fetchImpl } = fakeBackend();

    const result = await performPush({ chromeApi, storageArea, fetchImpl, now: () => 1000 });

    expect(state.currentCookie).toBe('sessionid_ss=good-session-value-1; passport_csrf_token=good-csrf-value-1');
    expect(result.ok).toBe(true);
    expect(result.skipped).toBe(false);
  });

  // THE GUARD (phase-09 §9.3): an empty cookie list must never overwrite a
  // good cookie already sitting on the backend. Asserted as an OUTCOME (the
  // backend's stored cookie is provably unchanged across a real push cycle),
  // not merely "fetch was not called" -- see test/mutations/phase-09.sh for
  // the mutation that proves this assertion is load-bearing.
  test('skips the push when chrome.cookies.getAll returns an empty list, leaving the backend cookie untouched', async () => {
    const storageArea = fakeStorageArea({ backendUrl: 'http://backend', apiKey: 'k' });
    const { state, fetchImpl } = fakeBackend();

    // First: a real push establishes a good cookie on the backend.
    await performPush({ chromeApi: fakeChromeApi(LOGGED_IN_COOKIES), storageArea, fetchImpl, now: () => 1000 });
    expect(state.currentCookie).toBe('sessionid_ss=good-session-value-1; passport_csrf_token=good-csrf-value-1');
    expect(state.pushCount).toBe(1);

    // Then: user logs out of Douyin -- chrome.cookies.getAll now returns [].
    const result = await performPush({ chromeApi: fakeChromeApi([]), storageArea, fetchImpl, now: () => 2000 });

    // Outcome: the backend's cookie is EXACTLY what it was before -- not
    // overwritten with '' or with a header built from an empty array.
    expect(state.currentCookie).toBe('sessionid_ss=good-session-value-1; passport_csrf_token=good-csrf-value-1');
    expect(state.pushCount).toBe(1); // no second POST was ever sent
    expect(result.skipped).toBe(true);
    expect(result.ok).toBe(false);
  });

  // Should-fix from code review: a logged-out .douyin.com is not guaranteed
  // to return an EMPTY array -- consent/locale/analytics cookies commonly
  // outlive the session, so the array can be non-empty while the actual
  // auth cookie (sessionid_ss) is gone. A pure `length === 0` guard misses
  // this shape entirely and would push a header built from leftover cookies
  // that carries no session, overwriting the good one on the backend.
  test('skips the push when cookies are present but sessionid_ss is missing (stale non-auth cookies after logout)', async () => {
    const storageArea = fakeStorageArea({ backendUrl: 'http://backend', apiKey: 'k' });
    const { state, fetchImpl } = fakeBackend();

    await performPush({ chromeApi: fakeChromeApi(LOGGED_IN_COOKIES), storageArea, fetchImpl, now: () => 1000 });
    expect(state.currentCookie).toBe('sessionid_ss=good-session-value-1; passport_csrf_token=good-csrf-value-1');

    const LEFTOVER_NON_AUTH_COOKIES = [
      { name: 'douyin_locale', value: 'en' },
      { name: 'consent_given', value: '1' },
    ];
    const result = await performPush({
      chromeApi: fakeChromeApi(LEFTOVER_NON_AUTH_COOKIES),
      storageArea,
      fetchImpl,
      now: () => 2000,
    });

    expect(state.currentCookie).toBe('sessionid_ss=good-session-value-1; passport_csrf_token=good-csrf-value-1');
    expect(state.pushCount).toBe(1);
    expect(result.skipped).toBe(true);
    expect(result.ok).toBe(false);
  });

  test('records a clear, non-generic error message when the backend rejects the API key (401)', async () => {
    const storageArea = fakeStorageArea({ backendUrl: 'http://backend', apiKey: 'wrong-key' });
    const chromeApi = fakeChromeApi(LOGGED_IN_COOKIES);
    const fetchImpl = async () => ({
      ok: false,
      status: 401,
      json: async () => ({}),
      text: async () => 'Unauthorized',
    });

    const result = await performPush({ chromeApi, storageArea, fetchImpl, now: () => 1000 });

    expect(result.ok).toBe(false);
    expect(result.skipped).toBe(false);
    // Exact match, not just /401/: a fallback "generic HTTP error" message
    // also happens to contain "401" (see lib/api-client.js's assertOk),
    // so a loose regex here would NOT catch the 401 branch being dropped.
    // This is the assertion test/mutations/phase-09.sh's "assertOk becomes
    // a no-op" mutation is aimed at.
    expect(result.message).toBe('backend rejected the API key (401 Unauthorized)');
  });

  test('every recorded result is persisted to storage so options.html can read it back later', async () => {
    const storageArea = fakeStorageArea({ backendUrl: 'http://backend', apiKey: 'k' });
    const { fetchImpl } = fakeBackend();

    await performPush({ chromeApi: fakeChromeApi(LOGGED_IN_COOKIES), storageArea, fetchImpl, now: () => 1000 });

    const { getSettings } = require('../lib/storage');
    const settings = await getSettings(storageArea);
    expect(settings.lastPushResult).toEqual({ at: expect.any(String), ok: true, skipped: false, message: 'pushed successfully' });
  });

  test('exposes PLATFORM as "douyin" -- the only platform the backend allowlists', () => {
    expect(PLATFORM).toBe('douyin');
  });
});
