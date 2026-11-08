'use strict';

/**
 * sw.js is a classic (non-module) service worker script that expects to run
 * inside a real ServiceWorkerGlobalScope, where `importScripts`, `chrome`,
 * and `fetch` are ambient globals. To load it under Jest we:
 *   1. stub `global.chrome` with capturing listeners BEFORE requiring it
 *      (sw.js reads the bare `chrome` global at call time, same as it would
 *      read `self`/`chrome` in the real service worker),
 *   2. require the four lib files first so their functions land on
 *      globalThis (sw.js guards `importScripts` with a typeof check and
 *      skips it under Node -- see sw.js's first line),
 *   3. require sw.js exactly once for this whole file, since Node caches
 *      `require()` and re-requiring would not re-run its top-level wiring
 *      against a fresh chrome stub.
 */

let onAlarmListener;
let onInstalledListener;
let onStartupListener;
let onMessageListener;

const storageData = {};
const storageArea = {
  get(keys, cb) {
    const out = {};
    for (const k of keys) out[k] = storageData[k];
    cb(out);
  },
  set(items, cb) {
    Object.assign(storageData, items);
    cb();
  },
};

let currentCookies = [];
let lastFetchCall = null;

global.chrome = {
  alarms: {
    create: jest.fn(),
    onAlarm: { addListener: (fn) => { onAlarmListener = fn; } },
  },
  runtime: {
    onInstalled: { addListener: (fn) => { onInstalledListener = fn; } },
    onStartup: { addListener: (fn) => { onStartupListener = fn; } },
    onMessage: { addListener: (fn) => { onMessageListener = fn; } },
    lastError: undefined,
  },
  storage: { local: storageArea },
  cookies: {
    getAll(details, cb) {
      cb(currentCookies);
    },
  },
};
global.fetch = async (url, init) => {
  lastFetchCall = { url, init };
  return { ok: true, status: 200, json: async () => ({ ok: true }), text: async () => '' };
};

require('../lib/cookie-header');
require('../lib/api-client');
require('../lib/storage');
require('../lib/push-cookie');
require('../lib/enqueue-video');
const sw = require('../sw.js');

beforeEach(() => {
  Object.assign(storageData, { backendUrl: 'http://backend', apiKey: 'k' });
  delete storageData.lastPushResult;
  currentCookies = [{ name: 'sessionid_ss', value: 'v1' }];
  lastFetchCall = null;
  global.chrome.alarms.create.mockClear();
});

describe('sw.js wiring', () => {
  test('exposes the 20-minute alarm config from phase-09 §9.3', () => {
    expect(sw.ALARM_PERIOD_MINUTES).toBe(20);
    expect(typeof sw.ALARM_NAME).toBe('string');
    expect(sw.ALARM_NAME.length).toBeGreaterThan(0);
  });

  test('onInstalled creates the alarm with the right name and 20-minute period, and triggers an immediate push', async () => {
    expect(onInstalledListener).toBeInstanceOf(Function);

    onInstalledListener();
    expect(chrome.alarms.create).toHaveBeenCalledWith(sw.ALARM_NAME, { periodInMinutes: 20 });

    await sw._lastRunPromise();
    expect(lastFetchCall).not.toBeNull();
    expect(lastFetchCall.url).toBe('http://backend/internal/cookie');
    expect(JSON.parse(lastFetchCall.init.body).cookie).toBe('sessionid_ss=v1');
  });

  test('onAlarm triggers a push only for the matching alarm name', async () => {
    expect(onAlarmListener).toBeInstanceOf(Function);

    lastFetchCall = null;
    onAlarmListener({ name: 'some-other-alarm' });
    // Flush microtasks so a wrongly-triggered push would have had a chance
    // to reach fetch by now.
    await Promise.resolve();
    await Promise.resolve();
    expect(lastFetchCall).toBeNull();

    onAlarmListener({ name: sw.ALARM_NAME });
    await sw._lastRunPromise();
    expect(lastFetchCall).not.toBeNull();
  });

  test('onMessage "push-now" runs a push and responds with the outcome, keeping the channel open', async () => {
    expect(onMessageListener).toBeInstanceOf(Function);

    const responsePromise = new Promise((resolve) => {
      const keepOpen = onMessageListener({ type: 'push-now' }, {}, resolve);
      expect(keepOpen).toBe(true);
    });

    const result = await responsePromise;
    expect(result.ok).toBe(true);
    expect(result.skipped).toBe(false);
  });

  test('onMessage ignores messages that are not "push-now"', () => {
    const sendResponse = jest.fn();
    const keepOpen = onMessageListener({ type: 'something-else' }, {}, sendResponse);
    expect(sendResponse).not.toHaveBeenCalled();
    expect(keepOpen).toBeUndefined();
  });

  test('onMessage "download-video" queues the given page URL and responds with the outcome, keeping the channel open', async () => {
    global.fetch = async (url, init) => {
      lastFetchCall = { url, init };
      return {
        ok: true,
        status: 202,
        json: async () => ({ aweme_id: '1', status: 'pending' }),
        text: async () => '',
      };
    };

    const response = await new Promise((resolve) => {
      const keepOpen = onMessageListener(
        { type: 'download-video', url: 'https://www.douyin.com/video/1' },
        {},
        resolve,
      );
      expect(keepOpen).toBe(true);
    });

    expect(response).toEqual({ ok: true, status: 'pending', message: 'queued' });
    expect(lastFetchCall.url).toBe('http://backend/api/videos');
    expect(JSON.parse(lastFetchCall.init.body)).toEqual({ url: 'https://www.douyin.com/video/1' });
  });
});
