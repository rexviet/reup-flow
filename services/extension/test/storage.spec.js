'use strict';

const { getSettings, setSettings, recordLastPushResult, STORAGE_KEYS } = require('../lib/storage');

/** In-memory stand-in for chrome.storage.local, same callback shape. */
function fakeStorageArea(initial) {
  const data = { ...initial };
  return {
    data,
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

describe('storage', () => {
  beforeEach(() => {
    global.chrome = { runtime: {} };
  });

  test('getSettings returns empty defaults when nothing stored yet', async () => {
    const area = fakeStorageArea({});
    const settings = await getSettings(area);
    expect(settings).toEqual({ backendUrl: '', apiKey: '', lastPushResult: null });
  });

  test('setSettings persists backendUrl and apiKey; getSettings reads them back', async () => {
    const area = fakeStorageArea({});
    await setSettings(area, { backendUrl: 'http://localhost:3000', apiKey: 'k-123' });
    const settings = await getSettings(area);
    expect(settings.backendUrl).toBe('http://localhost:3000');
    expect(settings.apiKey).toBe('k-123');
  });

  test('recordLastPushResult persists a result object and getSettings surfaces it', async () => {
    const area = fakeStorageArea({});
    const result = { at: '2026-08-25T00:00:00.000Z', ok: true, skipped: false, message: 'pushed successfully' };
    await recordLastPushResult(area, result);
    const settings = await getSettings(area);
    expect(settings.lastPushResult).toEqual(result);
  });

  test('rejects when chrome.runtime.lastError is set on get', async () => {
    global.chrome = { runtime: { lastError: { message: 'boom' } } };
    const area = fakeStorageArea({});
    await expect(getSettings(area)).rejects.toThrow('boom');
  });

  test('STORAGE_KEYS exposes the stable key names', () => {
    expect(STORAGE_KEYS.BACKEND_URL).toBe('backendUrl');
    expect(STORAGE_KEYS.API_KEY).toBe('apiKey');
    expect(STORAGE_KEYS.LAST_PUSH_RESULT).toBe('lastPushResult');
  });
});
