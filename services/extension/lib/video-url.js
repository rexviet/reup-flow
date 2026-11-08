'use strict';

/**
 * True when a douyin.com page URL points at ONE video: `/video/<id>`, or any
 * page showing a video in a modal (`?modal_id=<id>`: feed, profile, search).
 * Mirrors the backend's `extractAwemeId` (services/downloader/src/videos/
 * video-url.ts) so the button only shows where `POST /api/videos` would accept
 * the URL. Short links and `/note/` image posts are deliberately not matched.
 */
const VIDEO_PATH = /^\/(?:share\/)?video\/\d+\/?$/;
const NUMERIC_ID = /^\d+$/;

function isVideoPageUrl(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch (_e) {
    return false;
  }
  if (VIDEO_PATH.test(url.pathname)) return true;
  const modalId = url.searchParams.get('modal_id');
  return modalId !== null && NUMERIC_ID.test(modalId);
}

// IIFE, not a bare `const root`: every script (service worker, options page,
// content script) evaluates into ONE shared global scope -- see
// lib/api-client.js for the failure this avoids.
(() => {
  const root = typeof self !== 'undefined' ? self : globalThis;
  root.isVideoPageUrl = isVideoPageUrl;
})();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { isVideoPageUrl };
}
