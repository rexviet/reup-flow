'use strict';

/**
 * Orchestrates one cookie-push cycle: read chrome.cookies -> guard -> build
 * header -> POST to backend -> record the outcome. This is the module
 * sw.js's alarm/onInstalled/onMessage handlers call.
 *
 * `chromeApi`, `storageArea`, `fetchImpl`, `now` are all injected so this
 * function has no hidden global dependency and runs identically under Jest
 * (stubs) and inside the real service worker (sw.js passes the real
 * `chrome` global, `chrome.storage.local`, and leaves `fetchImpl`/`now`
 * undefined so the real `fetch`/`Date.now` are used).
 */

const DOUYIN_COOKIE_DOMAIN = '.douyin.com';
const PLATFORM = 'douyin';

function nowIso(now) {
  const ms = typeof now === 'function' ? now() : Date.now();
  return new Date(ms).toISOString();
}

function getAllCookies(chromeApi) {
  return new Promise((resolve, reject) => {
    chromeApi.cookies.getAll({ domain: DOUYIN_COOKIE_DOMAIN }, (result) => {
      const err = chromeApi.runtime && chromeApi.runtime.lastError;
      if (err) {
        reject(new Error(err.message));
        return;
      }
      resolve(result);
    });
  });
}

async function performPush({ chromeApi, storageArea, fetchImpl, now }) {
  const root = typeof self !== 'undefined' ? self : globalThis;
  try {
    const settings = await root.getSettings(storageArea);
    const cookies = await getAllCookies(chromeApi);

    // GUARD (phase-09 §9.3, plan.md risk register "push cookie rỗng đè
    // cookie tốt"): the real backend (CookieService.push) unconditionally
    // overwrites whatever is in Redis with whatever this request carries --
    // nothing server-side protects against a bad write. An empty cookie
    // list means "probably logged out" (or a transient read glitch); a
    // NON-empty list that lacks `sessionid_ss` means the same thing --
    // consent/locale/analytics cookies commonly outlive the session, so
    // `.douyin.com` can still hand back a non-empty array after logout with
    // no auth cookie in it at all. Checking length alone would miss that
    // shape and push a header with no session in it, silently overwriting
    // a good cookie already on the backend. Neither case may ever reach the
    // backend. See test/push-cookie.spec.js's outcome-based guard tests and
    // test/mutations/phase-09.sh, which proves removing this branch turns
    // those tests red.
    const hasSessionCookie =
      Array.isArray(cookies) && cookies.some((c) => c && c.name === 'sessionid_ss');
    if (!hasSessionCookie) {
      const result = {
        at: nowIso(now),
        ok: false,
        skipped: true,
        message:
          'no douyin session cookie (sessionid_ss) found (logged out?) -- push skipped, existing backend cookie left untouched',
      };
      await root.recordLastPushResult(storageArea, result);
      return result;
    }

    const cookieHeader = root.buildCookieHeader(cookies);
    await root.pushCookie({
      baseUrl: settings.backendUrl,
      apiKey: settings.apiKey,
      platform: PLATFORM,
      cookie: cookieHeader,
      fetchImpl,
    });

    const result = { at: nowIso(now), ok: true, skipped: false, message: 'pushed successfully' };
    await root.recordLastPushResult(storageArea, result);
    return result;
  } catch (err) {
    // Never resolves to a throw: sw.js's alarm/onInstalled handlers have
    // nothing useful to do with a rejected promise, and options.html's
    // "last push result" panel (phase-09 §9.4) needs a result object either
    // way -- this is what lets it tell "extension chết" (this catch would
    // never even run) apart from "backend từ chối" (this branch, with the
    // backend's own message surfaced verbatim -- never the cookie itself,
    // see CookiePushApiError in lib/api-client.js).
    const result = {
      at: nowIso(now),
      ok: false,
      skipped: false,
      message: err && err.message ? err.message : String(err),
    };
    try {
      const root2 = typeof self !== 'undefined' ? self : globalThis;
      await root2.recordLastPushResult(storageArea, result);
    } catch (_e) {
      // storage itself failed; nothing more this function can do
    }
    return result;
  }
}

// IIFE, not a bare `const root`: `importScripts` (sw.js) and classic
// `<script>` tags (options.html) both evaluate every file into ONE shared
// global scope, so a top-level `const root` here collides with the identical
// line in every sibling lib -- `SyntaxError: Identifier 'root' has already
// been declared`, which aborts service-worker registration with
// "Status code: 15". Jest's `require()` gives each file its own module
// scope and therefore cannot reproduce this -- the Jest suite is structurally
// blind to it, so do not treat a green `pnpm test` as coverage of this bug.
(() => {
  const root = typeof self !== 'undefined' ? self : globalThis;
  root.performPush = performPush;
  root.DOUYIN_COOKIE_DOMAIN = DOUYIN_COOKIE_DOMAIN;
  root.PLATFORM = PLATFORM;
})();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { performPush, DOUYIN_COOKIE_DOMAIN, PLATFORM };
}
