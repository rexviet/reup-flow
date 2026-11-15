'use strict';

// The options-page click handlers are exercised with their real storage and
// permission helpers in test/options.spec.js, without a jsdom dependency.
//
// Never reads or renders the cookie value -- this page has no `cookies`
// permission use anywhere; it only ever sees {at, ok, skipped, message} via
// chrome.storage.local's lastPushResult (see lib/push-cookie.js).

document.addEventListener('DOMContentLoaded', () => {
  const backendUrlInput = document.getElementById('backendUrl');
  const apiKeyInput = document.getElementById('apiKey');
  const saveButton = document.getElementById('save');
  const pushNowButton = document.getElementById('pushNow');
  const savedNotice = document.getElementById('savedNotice');
  const permissionNotice = document.getElementById('permissionNotice');
  const statusLabelEl = document.getElementById('statusLabel');
  const timeLabelEl = document.getElementById('timeLabel');
  const messageLabelEl = document.getElementById('messageLabel');

  function renderResult(result) {
    const view = formatLastPushResult(result);
    statusLabelEl.textContent = view.statusLabel;
    timeLabelEl.textContent = view.timeLabel;
    messageLabelEl.textContent = view.messageLabel;
    statusLabelEl.className = 'status-' + view.statusLabel.toLowerCase().replace(/[^a-z]+/g, '-');
  }

  async function load() {
    const settings = await getSettings(chrome.storage.local);
    backendUrlInput.value = settings.backendUrl;
    apiKeyInput.value = settings.apiKey;
    renderResult(settings.lastPushResult);
  }

  // Chrome only exempts extension fetches from CORS for origins covered by
  // a granted host permission (manifest.json's host_permissions covers
  // *.douyin.com only, needed for chrome.cookies -- the operator-chosen
  // backend URL is arbitrary and can't be declared up front, so it's
  // requested at runtime via chrome.permissions, see lib/backend-permission.js).
  // MUST be called directly from a click handler: chrome.permissions.request
  // only works inside an active user gesture, so it can never be requested
  // from sw.js's alarm/onInstalled handlers -- only here, and again inside
  // sw.js's onMessage handler is too late (the gesture doesn't cross the
  // message-passing boundary).
  async function ensurePermissionOrExplain(backendUrl) {
    const result = await ensureBackendHostPermission(chrome.permissions, backendUrl);
    if (!result.granted) {
      permissionNotice.textContent =
        result.reason === 'invalid backend URL'
          ? 'Backend URL is not a valid http(s) URL.'
          : 'Chrome permission to reach this backend was not granted -- pushes will fail until you allow it.';
    } else {
      permissionNotice.textContent = '';
    }
    return result.granted;
  }

  saveButton.addEventListener('click', async () => {
    const backendUrl = backendUrlInput.value.trim();
    const apiKey = apiKeyInput.value.trim();
    const granted = await ensurePermissionOrExplain(backendUrl);
    await setSettings(chrome.storage.local, { backendUrl, apiKey });
    savedNotice.textContent = granted ? 'Saved.' : 'Saved (host permission still needed -- see above).';
    setTimeout(() => {
      savedNotice.textContent = '';
    }, 4000);
  });

  pushNowButton.addEventListener('click', async () => {
    pushNowButton.disabled = true;
    pushNowButton.textContent = 'Pushing…';
    const granted = await ensurePermissionOrExplain(backendUrlInput.value.trim());
    if (!granted) {
      pushNowButton.disabled = false;
      pushNowButton.textContent = 'Push now';
      return;
    }
    // The service worker reads storage, so save the same settings for which
    // this click just obtained permission before asking it to push.
    try {
      await setSettings(chrome.storage.local, {
        backendUrl: backendUrlInput.value.trim(),
        apiKey: apiKeyInput.value.trim(),
      });
    } catch (err) {
      pushNowButton.disabled = false;
      pushNowButton.textContent = 'Push now';
      renderResult({ at: new Date().toISOString(), ok: false, message: err.message });
      return;
    }
    chrome.runtime.sendMessage({ type: 'push-now' }, (result) => {
      pushNowButton.disabled = false;
      pushNowButton.textContent = 'Push now';
      renderResult(result);
    });
  });

  load();
});
