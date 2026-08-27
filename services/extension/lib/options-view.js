'use strict';

/**
 * Pure rendering logic for options.html's "last push result" panel
 * (phase-09 §9.4). Kept separate from options.js's DOM wiring so it can run
 * under Jest (no jsdom in this workspace) -- options.js itself is thin glue
 * with no independent logic, see its own top-of-file comment.
 *
 * Never receives, stores, or renders the cookie value: `result` (from
 * push-cookie.js's performPush) only ever carries {at, ok, skipped,
 * message}, never the cookie itself.
 */
function formatLastPushResult(result) {
  if (!result) {
    return { statusLabel: 'never pushed yet', timeLabel: '', messageLabel: '' };
  }
  const timeLabel = result.at ? new Date(result.at).toLocaleString() : '';
  const messageLabel = result.message || '';
  if (result.skipped) {
    return { statusLabel: 'SKIPPED', timeLabel, messageLabel };
  }
  return { statusLabel: result.ok ? 'OK' : 'ERROR', timeLabel, messageLabel };
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
  root.formatLastPushResult = formatLastPushResult;
})();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { formatLastPushResult };
}
