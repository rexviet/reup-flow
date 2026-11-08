'use strict';

/**
 * What the page button shows after a click: `{ kind: 'ok' | 'error', text }`.
 * Pure, so the wording per backend status is unit tested; content.js only
 * paints it. A video that was already downloaded or is already running must not
 * read like a fresh queue, and `failed` / `skipped` must not read like success.
 */
const STATUS_TEXT = {
  pending: { kind: 'ok', text: 'Queued for download' },
  downloading: { kind: 'ok', text: 'Already downloading' },
  done: { kind: 'ok', text: 'Already downloaded' },
  failed: { kind: 'error', text: 'Earlier download failed' },
  skipped: { kind: 'error', text: 'Skipped (image post or cancelled)' },
};

function describeEnqueueResult(result) {
  if (!result) return { kind: 'error', text: 'No reply from the extension' };
  if (!result.ok) return { kind: 'error', text: result.message || 'Request failed' };
  return STATUS_TEXT[result.status] || { kind: 'ok', text: `Queued (${result.status})` };
}

(() => {
  const root = typeof self !== 'undefined' ? self : globalThis;
  root.describeEnqueueResult = describeEnqueueResult;
})();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { describeEnqueueResult };
}
