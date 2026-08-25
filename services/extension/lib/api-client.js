'use strict';

/**
 * Thin HTTP client for the real backend contract in
 * services/downloader/src/cookie/{cookie.controller.ts,cookie.service.ts}:
 *   POST /internal/cookie        body {platform, cookie}  header X-Api-Key
 *   GET  /internal/cookie/status                          header X-Api-Key
 *
 * Never logs the cookie value, the response body, or the API key anywhere --
 * see test/no-console-log.spec.js, which greps this file (and every other
 * production file in this package) for any `console.*` call at all.
 */

class CookiePushApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'CookiePushApiError';
    this.status = status;
  }
}

function normalizeBaseUrl(baseUrl) {
  return String(baseUrl || '').replace(/\/+$/, '');
}

function resolveFetch(fetchImpl) {
  const impl = fetchImpl || (typeof fetch !== 'undefined' ? fetch : undefined);
  if (!impl) throw new CookiePushApiError('fetch is not available in this context', 0);
  return impl;
}

async function doRequest(url, init, fetchImpl) {
  const impl = resolveFetch(fetchImpl);
  try {
    return await impl(url, init);
  } catch (err) {
    throw new CookiePushApiError(
      `network error reaching backend: ${err && err.message ? err.message : String(err)}`,
      0,
    );
  }
}

/** Response bodies get surfaced into a message that ends up persisted to
 * chrome.storage.local and rendered on the options page -- capped so an
 * unexpected backend error shape (or anything else) can never turn this
 * call site into an unbounded-storage or render-garbage vector. */
const MAX_ERROR_BODY_CHARS = 300;

function truncate(text) {
  return text.length > MAX_ERROR_BODY_CHARS
    ? `${text.slice(0, MAX_ERROR_BODY_CHARS)}… (truncated)`
    : text;
}

/** Throws CookiePushApiError for any non-2xx response; never returns it. */
async function assertOk(response) {
  if (response.status === 401) {
    throw new CookiePushApiError('backend rejected the API key (401 Unauthorized)', 401);
  }
  if (!response.ok) {
    let bodyText = '';
    try {
      bodyText = truncate(await response.text());
    } catch (_e) {
      // best-effort only
    }
    throw new CookiePushApiError(
      `backend rejected the request (HTTP ${response.status})${bodyText ? `: ${bodyText}` : ''}`,
      response.status,
    );
  }
}

/**
 * Pushes the assembled Cookie header to `POST {baseUrl}/internal/cookie`.
 * Resolves with nothing on success; throws CookiePushApiError otherwise.
 */
async function pushCookie({ baseUrl, apiKey, platform, cookie, fetchImpl }) {
  const response = await doRequest(
    `${normalizeBaseUrl(baseUrl)}/internal/cookie`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Api-Key': apiKey,
      },
      body: JSON.stringify({ platform, cookie }),
    },
    fetchImpl,
  );
  await assertOk(response);
}

/** Fetches `GET {baseUrl}/internal/cookie/status` and returns the parsed JSON body. */
async function getCookieStatus({ baseUrl, apiKey, fetchImpl }) {
  const response = await doRequest(
    `${normalizeBaseUrl(baseUrl)}/internal/cookie/status`,
    {
      method: 'GET',
      headers: { 'X-Api-Key': apiKey },
    },
    fetchImpl,
  );
  await assertOk(response);
  return response.json();
}

const root = typeof self !== 'undefined' ? self : globalThis;
root.CookiePushApiError = CookiePushApiError;
root.pushCookie = pushCookie;
root.getCookieStatus = getCookieStatus;
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { CookiePushApiError, pushCookie, getCookieStatus };
}
