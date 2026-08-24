# Douyin Parser Fork — Operational Guide

**For:** When the parser stops working at 2am. When it's time for the weekly upstream pull. When cookie isn't rotating.

**Upstream:** [`Evil0ctal/Douyin_TikTok_Download_API`](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
**Our fork:** `src/Douyin_TikTok_Download_API/`
**Pull checklist:** `src/Douyin_TikTok_Download_API/PATCH_NOTES.md`

---

## The Cookie Flow — Why This Matters

The Douyin cookie never goes in the code, and never in any file that gets committed.

```
┌─ Redis (ephemeral, TTL 24h)
│    douyin:cookie:current
│
├─ Environment variable (fallback)
│    $DOUYIN_COOKIE_FALLBACK
│
└─ config.yaml (deliberately empty)
     Cookie: ""
     
     Request reads: Redis first → env fallback → empty config value
     If all three are empty, parser logs a warning and sends an empty header.
```

**Why this design:**
- A Douyin cookie is short-lived. The design (IDEA.md §8, solution design §7.11) has a Chrome MV3 extension pushing a fresh one every ~20 min via `chrome.alarms`. **That extension is a Phase 7 deliverable and does not exist yet** — today the key is set by hand, see below.
- The parser must see the new cookie on **the next request**, no restart.
- If the cookie ever lived in `config.yaml`, someone could commit it by accident.

---

## Rotating the Cookie at Runtime

The parser reads Redis on every request, so writing the key is the whole rotation — no restart, no API call, no `docker restart`.

**Until the Phase 7 extension exists, this is the only way the cookie gets set.** It is also the fallback when the extension breaks.

1. Get a fresh Douyin cookie (from your browser DevTools or Postman).

2. Set it in Redis:
   ```bash
   redis-cli -u $REDIS_URL SET douyin:cookie:current "<your-cookie-here>"
   ```
   (TTL is 24h; it will expire if not refreshed by the extension.)

3. The next Douyin parser request will use the new cookie. No restart needed.

**Verify the swap works:**

From the fork root (`src/Douyin_TikTok_Download_API/`):

```bash
.venv/bin/python ../../scripts/prove-cookie-swap.py
```

This runs a real `redis-py` client over a real socket. It proves the swap **without needing a Redis server** — the script creates its own RESP-compliant stub. If it passes, cookie rotation is working.

---

## Failure Modes and How to Tell Them Apart

### Redis Unreachable

**Symptoms:**
- Douyin requests still work (fallback is active).
- Warning in logs once per 60 seconds (time-suppressed, not a one-shot): `cookie_provider: Redis unavailable (ConnectionError), using fallback`.
- Health dashboard shows `_consecutive_failures` counter incrementing.

**What's happening:**
- Parser fell back to `$DOUYIN_COOKIE_FALLBACK` (or empty `config.yaml`).
- Requests continue until the fallback cookie expires.

**Fix:**
- Restore Redis connection.
- Or set `$DOUYIN_COOKIE_FALLBACK` to a live cookie (good for ~24h).

### Redis Miss AND No Fallback

**Symptoms:**
- **Distinct value-free warning:** `cookie_provider: resolved cookie is empty (Redis, DOUYIN_COOKIE_FALLBACK, and config.yaml are all empty)`.
- Douyin rejects the request immediately (because we sent an empty `Cookie` header).
- Looks identical in blocked-job rate to "Douyin rejected our valid cookie" — but the fix is opposite.

**Fix:**
- Set `$DOUYIN_COOKIE_FALLBACK` to a valid cookie, **or**
- Set the Redis key via the extension.

### Cookie Expired or Blocked by Douyin

**Symptoms:**
- Blocked-job rate rises.
- Parser logs show no cookie warnings.
- Cookie is present (not empty).

**What's happening:**
- Douyin's anti-bot check flagged the cookie.
- Parser will keep retrying with the same cookie until a new one arrives.

**Fix:**
- Rotate the cookie manually (see above) if the extension isn't pushing fast enough.
- Check if the Chrome extension is still running and logged into Douyin.

### Douyin Changed the `a_bogus` Algorithm

**Symptoms:**
- Sudden spike in `invalid_request` errors from Douyin (across all cookies, all IPs).
- Solution design §7.2 mentions `a_bogus` as a parameter that changes regularly.

**Fix:**
- Pull upstream:
  ```bash
  cd src/Douyin_TikTok_Download_API
  git fetch upstream main
  git rebase upstream/main
  ```
- Review the diff; follow the checklist in `PATCH_NOTES.md` (items 1–5).
- Verify the patch is still applied (our one line in `web_crawler.py` should still be there).
- Re-run tests.
- Push to the fork.

---

## Constraints Worth Knowing Before Phase 2/3

### Async Client is Bound to the Event Loop

The async cookie provider caches its `redis.asyncio` client **per event loop**, not as a singleton.

**Why it matters:**
- Uvicorn with `--reload` creates a new loop per reload.
- A worker that creates a fresh loop per task will get a rebuilt client.
- Two sequential `asyncio.run()` calls (like in `scripts/prove-cookie-swap.py`) require loop detection.

**If you see this error:** `RuntimeError: got Future attached to a different loop` — the client detect mismatch and rebuild. This is correct behavior, not a bug.

### Socket Timeouts Are Load-Bearing

```python
socket_timeout=1
socket_connect_timeout=1
```

These are **not tuning knobs.** Without them, a hung or unreachable Redis hangs:
- Every Douyin request (in the sync path).
- The entire asyncio event loop (in the async path, starving the parser's own health endpoint).

Do not remove them.

### requirements.txt Now Needs `redis`

The fork's `requirements.txt` includes `redis` (the sync client). The async variant (`redis.asyncio`) is part of the same package.

`requirements-dev.txt` covers test dependencies (`pytest`, etc.).

### `update_cookie()` Was Deleted by Design

Upstream has a `POST /api/hybrid/update_cookie` endpoint (in `app/api/endpoints/hybrid_parsing.py`). It called `DouyinWebCrawler.update_cookie()`, which:
- Wrote the cookie to `config.yaml`.
- Printed it to stdout **three times**.
- Only worked for one parser instance.

**We deleted the entire method.** The endpoint now returns 500 (no method to call). This is intentional.

**Why:** We switched to pull-model (Redis). The cookie is pushed by the extension, not pulled by an API. The old push-to-file model had three fatal flaws: it leaked the cookie to logs, persisted it to a tracked file, and didn't scale to multiple parser instances.

If someone reports the endpoint 500ing, that is **expected, not a bug.**

---

## Running the Tests

From the fork root:

```bash
.venv/bin/python -m pytest tests -q
```

This runs **20 test cases** (19 test functions; `test_cookie_never_in_logs` is parametrized over both cookie sources) covering:

- Cookie swap (Redis key change → next request uses new cookie, no restart).
- Fallback behavior (Redis down → use env var or config).
- Empty cookie warning (all three sources empty → logged, no silent failure).
- Event loop safety (two loops in same process → rebuilt client per loop).
- Timeout enforcement (CI fails if `socket_timeout` or `socket_connect_timeout` is removed).
- No cookie in logs (CI asserts cookie never reaches logging subsystem or stdout/stderr).
- No `update_cookie()` exists (CI asserts method doesn't exist, so merging it back fails).

**CI also runs mutation testing:** the suite deliberately breaks each invariant (e.g., removes a timeout) and requires the tests to fail. This catches a future edit that quietly defangs an assertion.

---

## Security: Never Log or Commit the Cookie

Three upstream `print()` calls that wrote the cookie to stdout have been removed. CI tests enforce:

- Cookie never reaches the logging subsystem (even via exception info).
- Cookie never reaches stdout or stderr.
- No path exists to commit the cookie into a tracked file.

The `config.yaml` is deliberately set to `Cookie: ""`, so even a mistake can't land a live credential.

---

## Health Check Integration

For Phase 2/3, the orchestrator reads cookie health from:

```
Redis key: douyin:cookie:meta
TTL: 24h
{
  "pushed_at": <timestamp>,
  "last_ok_at": <timestamp>,
  "sha256": "<hash>"
}
```

- `pushed_at`: When the extension last set the cookie.
- `last_ok_at`: When a parser request succeeded with this cookie.
- `sha256`: Hash of the cookie (for deduplication; don't log the value).

The orchestrator's health dashboard uses this to show a cookie badge:
- Green if `last_ok_at < 5 minutes`.
- Yellow if older.
- Red if missing.

---

## Troubleshooting Checklist

| Symptom | Check | Fix |
|---------|-------|-----|
| Parser works for one request, then all fail | Is the extension running? Is it still logged into Douyin? | Restart extension or re-login |
| "Cookie unavailable" warnings every ~60s | `redis-cli PING` from the same network | Restore Redis, or set `$DOUYIN_COOKIE_FALLBACK` |
| "Cookie is empty" warning | Are Redis, fallback env var, and config value all empty? | Set one of them |
| Douyin rejects every request (across all cookies) | Did Douyin change `a_bogus`? | Pull upstream, review `PATCH_NOTES.md` |
| Parser suddenly succeeds after failing | Did a new cookie arrive? Check `redis-cli GET douyin:cookie:current` | Normal — cookie rotated |
| "Got Future attached to different loop" error | Is the async client being reused across loop boundaries? | This is detected and fixed automatically; no action needed |

---

## Related Documentation

- **Pull upstream checklist:** `src/Douyin_TikTok_Download_API/PATCH_NOTES.md`
- **Full design:** `docs/reports/solution-design-2026-08-24-reup-pipeline.md` §7.2
- **Architecture overview:** `IDEA.md` §4 (Component 1 — Parser service)
