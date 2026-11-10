# Douyin Cookie Pusher — Chrome MV3 extension (phase 9)

## Why this exists

`document.cookie` cannot see HttpOnly cookies (`sessionid_ss`,
`passport_csrf_token`) — that is usually why a request "with cookies" still
fails. `chrome.cookies.getAll` can. **That is the entire reason this
extension exists.** It reads the real cookie store via `chrome.cookies` and
pushes the result to the downloader backend's `POST /internal/cookie` every
20 minutes (and immediately on install), so the parser fork always has a
fresh, complete cookie without anyone pasting one by hand.

## Layout

```
manifest.json     MV3 manifest: permissions cookies/alarms/storage, host
                   permission *://*.douyin.com/*, optional_host_permissions
                   for the operator-chosen backend URL (see below),
                   background service worker, options page.
sw.js              Service worker: chrome.alarms (20') + onInstalled +
                   onStartup + "push now" message handler. No independent
                   logic beyond wiring — see lib/push-cookie.js.
options.html/.js   Settings UI: backend URL, API key, "Push now" button,
                   and the last-push-result panel (phase-09 §9.4).
lib/cookie-header.js   Builds the `name=value; ...` header from
                       chrome.cookies.getAll's result shape.
lib/api-client.js     POST /internal/cookie, GET /internal/cookie/status —
                       matches services/downloader/src/cookie/* exactly
                       (header X-Api-Key, body {platform, cookie}).
lib/storage.js         chrome.storage.local wrapper (backend URL, API key,
                        last push result — never the cookie itself).
lib/backend-permission.js  Requests the host permission the backend URL
                            needs before any fetch to it (see "CORS and
                            host permissions" below).
lib/push-cookie.js     Orchestration + the sessionid_ss guard (phase-09 §9.3).
lib/options-view.js    Pure formatting for the "last push result" panel.
```

content.js         Content script on douyin.com: a small round download
                   button pinned to the top-right corner of the playing video on any
                   video page
                   (`/video/<id>` or `?modal_id=<id>`). A click messages
                   sw.js, which calls the backend's `POST /api/videos`.
lib/video-url.js, lib/button-view.js, lib/video-box.js, lib/enqueue-video.js
                   Page-URL check, button wording per backend status, which
                   <video> is the one being watched (and where it is drawn), and
                   the enqueue call (settings come from storage in the
                   service worker; the API key never reaches the page).

No build step. Every file above is plain JS loaded directly by the browser.

## Download button

Open a Douyin video (`douyin.com/video/<id>`, or any page that shows one in
a modal) and click the round button at the top-right. The video is queued in
the downloader and the same download lane as profile jobs fetches it into
storage. The button answers "Queued", "Already downloading", "Already
downloaded", "Earlier download failed" or "Skipped (image post or
cancelled)". It uses the backend URL and API key from the options page.
Not covered: short links (`v.douyin.com/...`) and the main feed, whose URL
does not carry the current video's id.

## Load unpacked (development)

1. Open `chrome://extensions`.
2. Enable **Developer mode** (top-right toggle).
3. Click **Load unpacked** and select this `services/extension/` directory.
4. Click the extension's **Details** → **Extension options** (or right-click
   the toolbar icon → Options) to open `options.html`.
5. Enter:
   - **Backend URL** — e.g. `http://localhost:3000` (no trailing slash needed).
   - **API key** — the value of `API_KEY` in the repo root `.env` used by
     `docker compose` (see `docker-compose.yml`'s `nest.environment.API_KEY`).
6. Click **Save** — Chrome will prompt for permission to access the backend
   host (see "CORS and host permissions" below); allow it.
7. Click **Push now**. This also saves the URL and API key currently shown in the form
   before asking the service worker to push; a separate Save click is optional.
8. Log in to `douyin.com` in a normal tab first if you haven't — otherwise
   there is nothing for `chrome.cookies` to read yet.

## How the push cycle works

1. `chrome.cookies.getAll({ domain: '.douyin.com' })` — reads every cookie
   for the domain and its subdomains, HttpOnly included.
2. **Guard:** if there is no `sessionid_ss` cookie in the result (logged
   out, or a transient read failure — note the list can be non-empty even
   when logged out, since consent/locale cookies commonly outlive the
   session), the push is skipped entirely. The backend overwrites
   unconditionally, so pushing a cookie with no session in it would erase a
   good one. See `lib/push-cookie.js` and `test/push-cookie.spec.js`.
3. Otherwise, the cookies are joined into a `name=value; name2=value2`
   header and POSTed to `{backendUrl}/internal/cookie` with
   `{ platform: 'douyin', cookie: <header> }` and header `X-Api-Key`.
4. The outcome (`{ at, ok, skipped, message }`) is written to
   `chrome.storage.local` and shown on the options page — this is what lets
   you tell "the extension isn't running" apart from "the backend rejected
   the push" (phase-09 §9.4).

## CORS and host permissions

`manifest.json`'s `host_permissions` only covers `*://*.douyin.com/*`
(needed for `chrome.cookies`). Chrome only exempts an extension's `fetch()`
calls from ordinary CORS enforcement for origins covered by a **granted**
host permission — the operator-configured backend URL is arbitrary
(`localhost`, an internal host, ...) and can't be declared statically, so it
is listed under `optional_host_permissions` and requested at runtime via
`chrome.permissions.request` (`lib/backend-permission.js`), triggered from
the **Save** and **Push now** click handlers in `options.js` — this only
works inside a real user gesture, so it can never be requested from `sw.js`'s
alarm/`onInstalled` handlers. If you skip granting it, pushes will fail with
a network-style error until you click Save or Push now again and accept the
prompt.

## Testing

```
cd services/extension
pnpm test               # unit tests, chrome.* APIs stubbed (no browser needed)
pnpm test:mutations     # mutation harness — proves each guard is load-bearing
```

`pnpm test:mutations` (`test/mutations/phase-09.sh`) deliberately breaks each
guard one at a time (disables the `sessionid_ss` guard, weakens it back to a
plain length check, inserts a `console.log` of the cookie header, neuters
the 401-error path) — 5 mutations total — and requires the corresponding
test to go **red**, then restores the file from a pristine snapshot. A green
mutation run means every guard was proven necessary, not just "the suite
passed."

`test/options.spec.js` exercises the real options-page click handlers with the
storage and permission helpers in a VM-backed DOM harness. It checks that Push
now uses the displayed settings, permission denial leaves saved settings
untouched, and storage failure prevents a push with stale settings. The Chrome
permission prompt and a real cookie push still need the manual checks below.

## Manual verification checklist (maps to phase-09 DoD — cannot be automated)

- [ ] **Load unpacked, log in to Douyin, grant the backend host permission
      prompt on Save, "Push now" → `/internal/cookie/status` reports
      `present: true` with a small `age_seconds`.**
      ```
      curl -s http://localhost:3000/internal/cookie/status -H "X-Api-Key: <key>"
      ```
  - [ ] `present` is `true`
  - [ ] `age_seconds` is small (just pushed)

- [ ] **The pushed cookie contains `sessionid_ss`** — this is the whole
      reason the extension exists (§9.1). Since the backend never echoes
      the cookie back, verify by temporarily adding a one-line log in a
      *local, uncommitted* copy of `cookie.service.ts` (never commit this),
      or by trusting `test/cookie-header.spec.js`'s fixture-based proof plus
      confirming step 1 above shows `present: true` right after a real
      Douyin login (a `sessionid_ss`-less cookie set would not authenticate
      the parser at all, which step below also confirms).

- [ ] **A job runs using the cookie the extension pushed — nobody pasted
      it by hand.** Push once via the extension, then start a downloader job
      against a real profile and confirm it does not immediately fail with
      an auth/cookie error.

- [ ] **Log out of Douyin → "Push now" is skipped, the old cookie is NOT
      overwritten with empty.** After logging out, click "Push now": the
      options page should show status `SKIPPED`, not `ERROR` or `OK`. Note
      Douyin may still leave a non-empty cookie set behind (consent/locale
      cookies) — the guard checks specifically for `sessionid_ss`, not just
      "any cookies", so this should still skip correctly.
      Re-check `/internal/cookie/status` — `present` and `age_seconds`
      must be unchanged from before the logout (age_seconds should be
      *larger* than right after the last real push, not reset to ~0).

- [ ] **Wrong API key → options page shows a clear error.** Temporarily
      change the API key field to something wrong, click "Push now": the
      panel should show status `ERROR` with the message
      `backend rejected the API key (401 Unauthorized)` — not a generic
      failure, and not silence.

- [ ] **`console.log` never touches a cookie.** Automated, not manual:
      `pnpm test` runs `test/no-console-log.spec.js`, which greps every
      production file (`sw.js`, `options.js`, `lib/*.js`) for any
      `console.*` call at all and fails if one exists.
