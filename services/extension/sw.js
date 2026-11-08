'use strict';

// Classic (non-module) MV3 service worker: manifest.json's
// `background.service_worker` has no `"type": "module"`, so `importScripts`
// is the right way to pull in the shared lib files -- and it only exists
// inside a real ServiceWorkerGlobalScope. Under Jest, the test requires the
// four lib files directly instead (see test/sw.spec.js), which populates
// the same globals this file reads (`performPush`, `chrome`, ...).
if (typeof importScripts === 'function') {
  importScripts(
    'lib/cookie-header.js',
    'lib/api-client.js',
    'lib/storage.js',
    'lib/push-cookie.js',
    'lib/enqueue-video.js',
  );
}

const ALARM_NAME = 'douyin-cookie-push';
const ALARM_PERIOD_MINUTES = 20; // phase-09 §9.3

// Bare module variable, overwritten by every trigger (alarm / onInstalled /
// onStartup / push-now message) with no locking between them. Considered
// and accepted, not an oversight: `performPush` is stateless per call and
// the backend write it produces is a plain Redis SET (last-write-wins,
// see CookieService.push) -- two overlapping pushes just mean one extra
// redundant POST with the same cookie, never a torn or inconsistent write.
let lastRunPromise = null;

/**
 * The one place that calls `performPush`. Never logs anything -- see
 * test/no-console-log.spec.js, which greps this whole file for `console.*`.
 */
function runPush() {
  lastRunPromise = performPush({ chromeApi: chrome, storageArea: chrome.storage.local });
  return lastRunPromise;
}

// Recurring push: chrome.alarms survives service-worker suspension, unlike
// a setInterval, which is exactly why phase-09 §9.3 specifies alarms.
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    runPush();
  }
});

// First push happens right at install, not 20 minutes later (phase-09
// §9.3 + Risks: "chrome.alarms không chạy khi browser đóng").
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD_MINUTES });
  runPush();
});

// Belt-and-suspenders for the same risk: also push on browser startup, in
// case the alarm's own 20-minute clock hasn't fired yet after a cold start.
if (chrome.runtime.onStartup) {
  chrome.runtime.onStartup.addListener(() => {
    runPush();
  });
}

// options.html's "Push now" button (phase-09 §9.4) sends this message and
// awaits the reply for immediate UI feedback; the outcome is ALSO persisted
// to chrome.storage.local by performPush itself, so it survives the options
// page being closed and reopened later.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === 'push-now') {
    runPush().then(sendResponse);
    return true; // keeps the async sendResponse channel open
  }
  // content.js's page button: queue the video the tab is showing. The
  // message carries only the page URL; settings come from storage here, so the
  // API key never reaches a page-world script.
  if (message && message.type === 'download-video') {
    performEnqueue({
      storageArea: chrome.storage.local,
      url: message.url,
    }).then(sendResponse);
    return true;
  }
  return undefined;
});

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    ALARM_NAME,
    ALARM_PERIOD_MINUTES,
    runPush,
    _lastRunPromise: () => lastRunPromise,
  };
}
