'use strict';

require('../lib/api-client');
require('../lib/storage');
const { performEnqueue } = require('../lib/enqueue-video');

function storageWith(data) {
  return {
    get(keys, cb) {
      const out = {};
      for (const k of keys) out[k] = data[k];
      cb(out);
    },
    set(items, cb) {
      Object.assign(data, items);
      cb();
    },
  };
}

const URL_ON_PAGE = 'https://www.douyin.com/video/7481199234537033018';

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) };
}

describe('performEnqueue', () => {
  test('POSTs the page URL to /api/videos with the API key and reports the backend status', async () => {
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url, init });
      return response(202, { aweme_id: '7481199234537033018', status: 'pending' });
    };

    const result = await performEnqueue({
      storageArea: storageWith({ backendUrl: 'http://backend/', apiKey: 'k' }),
      url: URL_ON_PAGE,
      fetchImpl,
    });

    expect(result).toEqual({ ok: true, status: 'pending', message: 'queued' });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://backend/api/videos');
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].init.headers['X-Api-Key']).toBe('k');
    expect(JSON.parse(calls[0].init.body)).toEqual({ url: URL_ON_PAGE });
  });

  test.each([
    ['backend URL', { apiKey: 'k' }],
    ['API key', { backendUrl: 'http://backend' }],
  ])('without a %s it fails before any request is made', async (_label, data) => {
    const fetchImpl = jest.fn();
    const result = await performEnqueue({ storageArea: storageWith(data), url: URL_ON_PAGE, fetchImpl });
    expect(result.ok).toBe(false);
    expect(result.message).toMatch(/options/i);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  test('a 401 resolves to a failed result naming the API key, never a throw', async () => {
    const result = await performEnqueue({
      storageArea: storageWith({ backendUrl: 'http://backend', apiKey: 'wrong' }),
      url: URL_ON_PAGE,
      fetchImpl: async () => response(401, {}),
    });
    expect(result).toEqual({ ok: false, message: 'backend rejected the API key (401 Unauthorized)' });
  });

  test('a network failure resolves to a failed result', async () => {
    const result = await performEnqueue({
      storageArea: storageWith({ backendUrl: 'http://backend', apiKey: 'k' }),
      url: URL_ON_PAGE,
      fetchImpl: async () => {
        throw new Error('connection refused');
      },
    });
    expect(result.ok).toBe(false);
    expect(result.message).toMatch(/connection refused/);
  });

  test('a backend reply without a status is not reported as queued', async () => {
    const result = await performEnqueue({
      storageArea: storageWith({ backendUrl: 'http://backend', apiKey: 'k' }),
      url: URL_ON_PAGE,
      fetchImpl: async () => response(202, {}),
    });
    expect(result.ok).toBe(false);
  });
});
