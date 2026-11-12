'use strict';

const { pushCookie, getCookieStatus, enqueueVideo, CookiePushApiError } = require('../lib/api-client');

/** Minimal fetch Response stub -- only what the client reads. */
function fakeResponse({ ok, status, jsonBody, textBody }) {
  return {
    ok,
    status,
    json: async () => jsonBody,
    text: async () => (textBody === undefined ? JSON.stringify(jsonBody || {}) : textBody),
  };
}

describe('pushCookie', () => {
  test('POSTs to {baseUrl}/internal/cookie with X-Api-Key header and {platform, cookie} JSON body', async () => {
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url, init });
      return fakeResponse({ ok: true, status: 200, jsonBody: { ok: true } });
    };

    await pushCookie({
      baseUrl: 'http://localhost:3000',
      apiKey: 'test-key-123',
      platform: 'douyin',
      cookie: 'sessionid_ss=abc; passport_csrf_token=def',
      fetchImpl,
    });

    expect(calls).toHaveLength(1);
    const { url, init } = calls[0];
    expect(url).toBe('http://localhost:3000/internal/cookie');
    expect(init.method).toBe('POST');
    expect(init.headers['X-Api-Key']).toBe('test-key-123');
    expect(JSON.parse(init.body)).toEqual({
      platform: 'douyin',
      cookie: 'sessionid_ss=abc; passport_csrf_token=def',
    });
  });

  test('strips a trailing slash from baseUrl before building the URL', async () => {
    const calls = [];
    const fetchImpl = async (url) => {
      calls.push(url);
      return fakeResponse({ ok: true, status: 200, jsonBody: { ok: true } });
    };
    await pushCookie({
      baseUrl: 'http://localhost:3000/',
      apiKey: 'k',
      platform: 'douyin',
      cookie: 'sessionid_ss=abc12345',
      fetchImpl,
    });
    expect(calls[0]).toBe('http://localhost:3000/internal/cookie');
  });

  test('throws CookiePushApiError with a clear message on 401 (wrong API key)', async () => {
    const fetchImpl = async () => fakeResponse({ ok: false, status: 401, textBody: 'Unauthorized' });

    await expect(
      pushCookie({ baseUrl: 'http://x', apiKey: 'wrong', platform: 'douyin', cookie: 'a=b', fetchImpl }),
    ).rejects.toThrow(CookiePushApiError);

    let caught;
    try {
      await pushCookie({ baseUrl: 'http://x', apiKey: 'wrong', platform: 'douyin', cookie: 'a=b', fetchImpl });
    } catch (err) {
      caught = err;
    }
    expect(caught.status).toBe(401);
    expect(caught.message).toMatch(/401/);
  });

  test('throws CookiePushApiError on a 400 (validation rejected)', async () => {
    const fetchImpl = async () =>
      fakeResponse({ ok: false, status: 400, textBody: 'cookie: cookie must be at least 8 characters' });

    await expect(
      pushCookie({ baseUrl: 'http://x', apiKey: 'k', platform: 'douyin', cookie: 'short', fetchImpl }),
    ).rejects.toThrow(CookiePushApiError);
  });

  // Should-fix from code review: a non-2xx response body is surfaced
  // verbatim into an error message that ends up persisted to
  // chrome.storage.local and rendered on the options page. Today's only
  // reachable case is a short, fixed zod message, but nothing bounds it if
  // the backend's error shape ever changes -- cap it so this call site can
  // never become an unbounded-storage or render-garbage vector.
  test('truncates an oversized error response body instead of storing it unbounded', async () => {
    const hugeBody = 'x'.repeat(5000);
    const fetchImpl = async () => fakeResponse({ ok: false, status: 500, textBody: hugeBody });

    let caught;
    try {
      await pushCookie({ baseUrl: 'http://x', apiKey: 'k', platform: 'douyin', cookie: 'a=bcdefgh', fetchImpl });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(CookiePushApiError);
    expect(caught.message.length).toBeLessThan(600);
  });

  test('throws CookiePushApiError when fetch itself rejects (network error)', async () => {
    const fetchImpl = async () => {
      throw new Error('getaddrinfo ENOTFOUND');
    };
    await expect(
      pushCookie({ baseUrl: 'http://x', apiKey: 'k', platform: 'douyin', cookie: 'a=b', fetchImpl }),
    ).rejects.toThrow(CookiePushApiError);
  });
});

describe('getCookieStatus', () => {
  test('GETs {baseUrl}/internal/cookie/status with X-Api-Key and returns the parsed body', async () => {
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url, init });
      return fakeResponse({
        ok: true,
        status: 200,
        jsonBody: { present: true, age_seconds: 12, last_ok_at: null },
      });
    };

    const status = await getCookieStatus({ baseUrl: 'http://localhost:3000', apiKey: 'k', fetchImpl });

    expect(calls[0].url).toBe('http://localhost:3000/internal/cookie/status');
    expect(calls[0].init.headers['X-Api-Key']).toBe('k');
    expect(status).toEqual({ present: true, age_seconds: 12, last_ok_at: null });
  });

  test('throws CookiePushApiError on 401', async () => {
    const fetchImpl = async () => fakeResponse({ ok: false, status: 401, textBody: 'Unauthorized' });
    await expect(getCookieStatus({ baseUrl: 'http://x', apiKey: 'wrong', fetchImpl })).rejects.toThrow(
      CookiePushApiError,
    );
  });
});

describe('enqueueVideo', () => {
  test('POSTs {url, localize: true} to {baseUrl}/api/videos with X-Api-Key and returns the parsed body', async () => {
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url, init });
      return fakeResponse({ ok: true, status: 202, jsonBody: { aweme_id: '1', status: 'pending' } });
    };

    const body = await enqueueVideo({
      baseUrl: 'http://localhost:3000/',
      apiKey: 'key',
      url: 'https://www.douyin.com/video/1',
      fetchImpl,
    });

    expect(body).toEqual({ aweme_id: '1', status: 'pending' });
    expect(calls[0].url).toBe('http://localhost:3000/api/videos');
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].init.headers['X-Api-Key']).toBe('key');
    expect(JSON.parse(calls[0].init.body)).toEqual({
      url: 'https://www.douyin.com/video/1',
      localize: true,
    });
  });

  test('throws CookiePushApiError carrying the status for a non-2xx reply', async () => {
    const fetchImpl = async () => fakeResponse({ ok: false, status: 404, textBody: 'video not found' });
    await expect(
      enqueueVideo({ baseUrl: 'http://b', apiKey: 'k', url: 'https://www.douyin.com/video/1', fetchImpl }),
    ).rejects.toMatchObject({ name: 'CookiePushApiError', status: 404 });
  });
});
