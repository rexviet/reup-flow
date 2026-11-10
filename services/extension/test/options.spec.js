'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

async function optionsPage({ granted = true, storageError = false } = {}) {
  const stored = { backendUrl: 'http://localhost:3000', apiKey: 'old-test-key' };
  const elements = new Map();
  let ready;
  const document = {
    addEventListener(_event, handler) { ready = handler; },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        value: '', textContent: '', disabled: false,
        addEventListener(event, handler) { this[event] = handler; },
      });
      return elements.get(id);
    },
  };
  const sent = [];
  const chrome = {
    storage: { local: {
      get(_keys, callback) { callback({ ...stored }); },
      set(values, callback) {
        if (storageError) chrome.runtime.lastError = { message: 'storage unavailable' };
        else Object.assign(stored, values);
        callback();
        delete chrome.runtime.lastError;
      },
    } },
    permissions: {
      contains(_details, callback) { callback(granted); },
      request(_details, callback) { callback(granted); },
    },
    runtime: {
      sendMessage(message, callback) {
        sent.push({ message, backendUrl: stored.backendUrl, apiKey: stored.apiKey });
        callback({ ok: true, message: 'pushed successfully' });
      },
    },
  };
  const context = vm.createContext({ document, chrome, URL, setTimeout });
  for (const file of ['lib/storage.js', 'lib/backend-permission.js', 'lib/options-view.js', 'options.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), context);
  }
  ready();
  await Promise.resolve();
  elements.get('backendUrl').value = ' http://localhost:3300 ';
  elements.get('apiKey').value = ' new-test-key ';
  return { elements, stored, sent };
}

test('Push now uses the visible URL and key without a separate Save click', async () => {
  const { elements, sent } = await optionsPage();
  await elements.get('pushNow').click();
  expect(sent).toEqual([{
    message: { type: 'push-now' },
    backendUrl: 'http://localhost:3300',
    apiKey: 'new-test-key',
  }]);
  expect(elements.get('statusLabel').textContent).toBe('OK');
  expect(elements.get('pushNow').disabled).toBe(false);
});

test('a denied host permission does not save settings or push', async () => {
  const { elements, stored, sent } = await optionsPage({ granted: false });
  await elements.get('pushNow').click();
  expect(sent).toEqual([]);
  expect(stored.backendUrl).toBe('http://localhost:3000');
  expect(elements.get('pushNow').disabled).toBe(false);
});

test('a settings write failure does not push stale settings and restores the button', async () => {
  const { elements, sent } = await optionsPage({ storageError: true });
  await elements.get('pushNow').click();
  expect(sent).toEqual([]);
  expect(elements.get('statusLabel').textContent).toBe('ERROR');
  expect(elements.get('messageLabel').textContent).toBe('storage unavailable');
  expect(elements.get('pushNow').disabled).toBe(false);
});
