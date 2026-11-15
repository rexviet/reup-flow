'use strict';

/**
 * One "download this video" click: read the saved backend settings, POST the
 * page URL to `/api/videos`, and answer `{ ok, status?, message }`.
 *
 * Never rejects -- sw.js hands the result straight back to the content script
 * that asked, and a rejected promise there would leave the button spinning.
 * Nothing here touches a cookie; the backend already has the session cookie
 * the other half of this extension pushed.
 */
async function performEnqueue({ storageArea, url, fetchImpl }) {
  const root = typeof self !== 'undefined' ? self : globalThis;
  try {
    const settings = await root.getSettings(storageArea);
    if (!settings.backendUrl || !settings.apiKey) {
      return { ok: false, message: 'set the backend URL and API key in the extension options first' };
    }
    const body = await root.enqueueVideo({
      baseUrl: settings.backendUrl,
      apiKey: settings.apiKey,
      url,
      fetchImpl,
    });
    if (!body || typeof body.status !== 'string') {
      return { ok: false, message: 'backend answered without a video status' };
    }
    return { ok: true, status: body.status, message: 'queued' };
  } catch (err) {
    return { ok: false, message: err && err.message ? err.message : String(err) };
  }
}

(() => {
  const root = typeof self !== 'undefined' ? self : globalThis;
  root.performEnqueue = performEnqueue;
})();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { performEnqueue };
}
