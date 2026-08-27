'use strict';

/**
 * Thin wrapper around `chrome.storage.local` (callback-style API). Never
 * stores the cookie value itself -- only backend URL, API key, and the
 * *result* of the last push (timestamp + ok/skipped + a message; see
 * push-cookie.js), so options.html can show "last push: 09:41, ok" without
 * ever holding the cookie in extension state longer than one push cycle.
 */

const STORAGE_KEYS = {
  BACKEND_URL: 'backendUrl',
  API_KEY: 'apiKey',
  LAST_PUSH_RESULT: 'lastPushResult',
};

function lastError() {
  return typeof chrome !== 'undefined' && chrome.runtime ? chrome.runtime.lastError : undefined;
}

function getSettings(storageArea) {
  return new Promise((resolve, reject) => {
    storageArea.get(
      [STORAGE_KEYS.BACKEND_URL, STORAGE_KEYS.API_KEY, STORAGE_KEYS.LAST_PUSH_RESULT],
      (items) => {
        const err = lastError();
        if (err) {
          reject(new Error(err.message));
          return;
        }
        resolve({
          backendUrl: items[STORAGE_KEYS.BACKEND_URL] || '',
          apiKey: items[STORAGE_KEYS.API_KEY] || '',
          lastPushResult: items[STORAGE_KEYS.LAST_PUSH_RESULT] || null,
        });
      },
    );
  });
}

function setSettings(storageArea, { backendUrl, apiKey }) {
  return new Promise((resolve, reject) => {
    storageArea.set(
      { [STORAGE_KEYS.BACKEND_URL]: backendUrl, [STORAGE_KEYS.API_KEY]: apiKey },
      () => {
        const err = lastError();
        if (err) {
          reject(new Error(err.message));
          return;
        }
        resolve();
      },
    );
  });
}

function recordLastPushResult(storageArea, result) {
  return new Promise((resolve, reject) => {
    storageArea.set({ [STORAGE_KEYS.LAST_PUSH_RESULT]: result }, () => {
      const err = lastError();
      if (err) {
        reject(new Error(err.message));
        return;
      }
      resolve();
    });
  });
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
  root.STORAGE_KEYS = STORAGE_KEYS;
  root.getSettings = getSettings;
  root.setSettings = setSettings;
  root.recordLastPushResult = recordLastPushResult;
})();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { STORAGE_KEYS, getSettings, setSettings, recordLastPushResult };
}
