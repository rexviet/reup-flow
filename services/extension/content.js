'use strict';
// skip-tdd: DOM-only wiring, like options.js (this workspace has no jsdom).
// Every decision -- is this a video page, what to say after a click -- lives
// in lib/video-url.js, lib/button-view.js and lib/video-box.js, all unit tested.

// Runs on every douyin.com page, which is a single-page app: the URL changes
// without a reload, and a content script cannot see the page's history calls
// from its isolated world. So the URL is polled; the check is one regex.
(() => {
  const BUTTON_ID = 'reup-flow-download-button';
  const POLL_MS = 250;
  const INSET = 12;
  const MESSAGE_MS = 4000;

  const COLORS = { idle: '#fe2c55', ok: '#1f9d55', error: '#c0392b' };
  const ICON =
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#fff" stroke-width="2.2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="M7 11l5 5 5-5"/>' +
    '<path d="M5 20h14"/></svg>';

  let wrapper = null;
  let button = null;
  let note = null;
  let busy = false;
  let noteTimer = null;

  function build() {
    wrapper = document.createElement('div');
    wrapper.id = BUTTON_ID;
    wrapper.style.cssText =
      'position:fixed;top:12px;left:100%;transform:translateX(-100%);z-index:2147483647;display:flex;' +
      'align-items:center;gap:8px;font:12px/1.2 system-ui,sans-serif;';

    note = document.createElement('span');
    note.style.cssText =
      'display:none;max-width:260px;padding:6px 10px;border-radius:6px;color:#fff;background:#333;';

    button = document.createElement('button');
    button.type = 'button';
    button.title = 'Download this video';
    button.innerHTML = ICON;
    button.style.cssText =
      'width:32px;height:32px;padding:0;border:0;border-radius:50%;cursor:pointer;display:flex;' +
      `align-items:center;justify-content:center;background:${COLORS.idle};` +
      'box-shadow:0 1px 4px rgba(0,0,0,.4);';
    button.addEventListener('click', onClick);

    wrapper.append(note, button);
    document.documentElement.appendChild(wrapper);
  }

  function show(text, kind) {
    note.textContent = text;
    note.style.background = COLORS[kind] || '#333';
    note.style.display = 'block';
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => {
      note.style.display = 'none';
    }, MESSAGE_MS);
  }

  function onClick() {
    if (busy) return;
    busy = true;
    button.style.opacity = '0.6';
    button.style.cursor = 'progress';
    const finish = (reply) => {
      busy = false;
      button.style.opacity = '1';
      button.style.cursor = 'pointer';
      const view = describeEnqueueResult(reply);
      show(view.text, view.kind);
    };
    try {
      chrome.runtime.sendMessage({ type: 'download-video', url: location.href }, (reply) => {
        // lastError is read so Chrome does not log "Unchecked runtime.lastError"
        // when the worker did not answer; the empty reply then reads as an error.
        void chrome.runtime.lastError;
        finish(reply);
      });
    } catch (_e) {
      // "Extension context invalidated": the extension was reloaded under this tab.
      finish({ ok: false, message: 'extension was reloaded -- refresh this page' });
    }
  }

  // Pins the button's right edge to the top-right corner of the video being
  // watched. With no <video> that qualifies (still loading) it falls back to the
  // window's own corner rather than disappearing.
  function place() {
    const candidates = Array.from(document.querySelectorAll('video')).map((v) => ({
      rect: v.getBoundingClientRect(),
      videoWidth: v.videoWidth,
      videoHeight: v.videoHeight,
    }));
    const box = pickVideoBox(candidates, { width: window.innerWidth, height: window.innerHeight });
    const right = box ? box.right : window.innerWidth;
    const top = box ? box.top : 0;
    wrapper.style.left = `${right - INSET}px`;
    wrapper.style.top = `${top + INSET}px`;
  }

  function sync() {
    const wanted = isVideoPageUrl(location.href);
    if (wanted && !wrapper) build();
    if (!wrapper) return;
    wrapper.style.display = wanted ? 'flex' : 'none';
    if (wanted) place();
  }

  if (document.getElementById(BUTTON_ID)) return;
  sync();
  setInterval(sync, POLL_MS);
})();
