'use strict';

/**
 * Chrome exempts extension-initiated `fetch()` calls from CORS ONLY for
 * origins covered by a granted host permission -- declared statically in
 * `manifest.json`'s `host_permissions`, or dynamically via
 * `chrome.permissions.request`. This extension's `host_permissions` only
 * covers `*://*.douyin.com/*` (needed for `chrome.cookies`); the
 * operator-configured backend URL is arbitrary (localhost, an internal
 * host, ...) and cannot be known at manifest-write time, so it is listed
 * under `optional_host_permissions` instead and requested here at runtime.
 *
 * `chrome.permissions.request` only works from inside a real user gesture
 * (a click handler), so this must be called directly from options.js's
 * Save/Push-now handlers -- never from sw.js's alarm/onInstalled handlers,
 * which have no gesture to spend. See options.js.
 */

function originPatternFor(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch (_e) {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  return `${url.protocol}//${url.host}/*`;
}

/**
 * Resolves `{ granted, pattern, reason? }`. Never throws -- an invalid URL
 * or a denied prompt both come back as `granted: false` with a `reason`,
 * so callers (options.js) can render a clear message either way.
 */
function ensureBackendHostPermission(permissionsApi, backendUrl) {
  const pattern = originPatternFor(backendUrl);
  if (!pattern) {
    return Promise.resolve({ granted: false, pattern: null, reason: 'invalid backend URL' });
  }
  return new Promise((resolve) => {
    permissionsApi.contains({ origins: [pattern] }, (alreadyGranted) => {
      if (alreadyGranted) {
        resolve({ granted: true, pattern });
        return;
      }
      permissionsApi.request({ origins: [pattern] }, (granted) => {
        resolve({
          granted,
          pattern,
          reason: granted ? null : 'permission request denied -- Chrome cannot reach this backend without it',
        });
      });
    });
  });
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
  root.originPatternFor = originPatternFor;
  root.ensureBackendHostPermission = ensureBackendHostPermission;
})();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { originPatternFor, ensureBackendHostPermission };
}
