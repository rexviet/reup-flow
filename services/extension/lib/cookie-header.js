'use strict';

/**
 * Builds the `name=value; name2=value2` Cookie header string from the shape
 * `chrome.cookies.getAll` returns.
 *
 * This is the entire reason the extension exists (phase-09 spec §9.1):
 * `document.cookie` cannot see HttpOnly cookies like `sessionid_ss` /
 * `passport_csrf_token`, but `chrome.cookies.getAll` returns them because it
 * reads the browser's cookie store directly, not the DOM.
 *
 * Returns null when there is nothing to push. Callers MUST treat null as "do
 * not push" -- see push-cookie.js's empty-list guard (phase-09 §9.3): an
 * empty/absent cookie list means "probably logged out", never "the cookie is
 * now empty", and must never overwrite a good cookie already on the backend.
 */
function buildCookieHeader(cookies) {
  if (!Array.isArray(cookies) || cookies.length === 0) return null;
  return cookies.map((c) => `${c.name}=${c.value}`).join('; ');
}

// Dual export: `self.buildCookieHeader` for classic-script `importScripts()`
// use inside the service worker / options page, `module.exports` for Jest
// (Node has no `self`; `self` also equals `window` in an extension page, so
// this one guard covers every real runtime this file loads in).
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
  root.buildCookieHeader = buildCookieHeader;
})();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { buildCookieHeader };
}
