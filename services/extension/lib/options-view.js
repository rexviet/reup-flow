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

const root = typeof self !== 'undefined' ? self : globalThis;
root.formatLastPushResult = formatLastPushResult;
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { formatLastPushResult };
}
