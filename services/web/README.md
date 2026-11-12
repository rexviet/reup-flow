# `services/web` — Douyin Downloader Web UI

One dashboard page plus a video detail page (`#/video/<aweme>`), React + Vite +
Tailwind + TypeScript, talking to `services/downloader`'s HTTP API (`api/dashboard/*`,
`api/jobs`, `api/videos`, `api/profiles`, `internal/cookie`). Requests use relative
paths: leave `VITE_API_BASE_URL` empty and `pnpm dev` proxies `/api` and `/internal` to
`127.0.0.1:3300`; the compose image does the same through nginx.

## Run locally

```bash
cd services/web
cp .env.example .env.local   # VITE_API_KEY; keep VITE_API_BASE_URL empty (same origin)
pnpm install                 # from repo root: pnpm install --filter @reup-flow/web...
pnpm dev                     # http://localhost:5173
```

`VITE_API_KEY` must match `services/downloader`'s `API_KEY` — every route on
that service, `/internal/*` included, is behind the global `X-Api-Key` guard
(`services/downloader/src/auth/api-key.guard.ts`).

## Test / lint / build

```bash
pnpm test     # vitest run -- 38 tests as of this phase
pnpm lint     # eslint
pnpm build    # tsc --noEmit && vite build -> dist/
```

Tests are colocated (`*.test.ts(x)` next to the file they cover) and mock
`fetch` / the `api/client` module rather than requiring a live backend --
see "Manual verification against the real API" below for what was and
wasn't checked against the actual `services/downloader` container.

## Why two separate progress bars (§8.2)

`JobResponse.total_videos` (`src/api/types.ts`, mirrored from
`services/downloader/src/jobs/jobs.mapper.ts`) is `number | null`. It is
`NULL` until the enumerate lane finishes at least one full pass over the
profile (`finishEnumerate` in `jobs.repository.ts`) -- **the denominator for
a combined percentage does not exist until then.** A single merged bar would
have to either divide by zero or substitute a guessed total, and either way
the displayed number would visibly **jump backwards** the moment enumerate
turns up more videos than the guess assumed.

`src/components/ProgressBars.tsx` therefore always renders **two** distinct
`role="progressbar"` elements:

1. **Enumerate bar** — `"Đang quét: N video"`. Renders **indeterminate**
   (no `aria-valuenow`, just a moving stripe) while `total_videos === null`.
2. **Download bar** — `"downloaded / total"`. Renders a placeholder
   (`"N / ?"`) instead of a bogus ratio while `total_videos` is still null.

**Do not merge these into one bar.** See the comment directly above
`ProgressBars` and the plan's Risks table (phase-08-web-ui.md, row "Ai đó
gộp 2 thanh thành 1 %"). `src/components/ProgressBars.test.tsx` mutation-
tests this: forcing the enumerate bar to always render determinate flips
`ProgressBars -- enumerate bar > is indeterminate ...` red.

## F7 — "340/340 ngay lập tức" is correct, not a bug (§8.4)

`videos.aweme_id` is a global key (not scoped to a job) — see F7 in
`plans/260824-2152-douyin-downloader/plan.md` §2. Re-running an
already-fully-downloaded profile can therefore show `340/340` on the very
first poll of the new job, because `total_videos`/`downloaded_count` reflect
**profile-wide** state a prior job already finished, while
`enumerated_count` for THIS job can land at 0.

`ProgressBars` shows a `"0 video mới"` label whenever
`job.enumerated_count === 0` (gated on `job.status !== 'pending'` so a brand
new, not-yet-started job doesn't show it prematurely — see the comment in
the component). This is the only thing standing between correct behaviour
and an operator filing "340/340 immediately" as a bug. Covered by
`ProgressBars.test.tsx`'s `F7 "0 video mới" label` describe block, and
mutation-tested (forcing the label off flips the completed-rerun test red).

## Cookie badge (§8.3)

`src/components/CookieBadge.tsx`, rendered in `App.tsx`'s header — always
visible, never behind a menu, because most `blocked` jobs are a cookie
problem and this is meant to answer that in one glance.

| State | Condition |
|---|---|
| 🔴 red | `present === false` |
| 🟡 amber | present, `age_seconds === null` OR `age_seconds >= 1800` (30 min) |
| 🟢 green | present, `age_seconds < 1800` |

Mutation-tested: flipping the `!status.present` branch to return `'green'`
flips `cookieBadgeState > is red when the cookie is not present` red.

## No cookie-paste input (§8.5)

Deliberate. Cookies are pushed with `curl -X POST /internal/cookie` until
the extension (phase 9) exists. Building a paste box here would be work
thrown away the moment M3 lands — see plan.md §7's deviation table.

## Polling (§8.1)

Both the job list and cookie status poll every 1s (`src/hooks/usePolling.ts`,
`POLL_INTERVAL_MS` in `App.tsx`). A failed tick sets `error` but does **not**
clear the last-known-good `data` and does **not** stop the interval — the
next tick just tries again with no reconnect logic needed. Mutation-tested:
making a failed tick also clear `data` flips the "DoD: polling continues
after one tick fails" test red.

`CANCELLABLE_STATUSES` / `RETRYABLE_STATUSES` in `src/api/types.ts` mirror
`services/downloader/src/db/jobs.repository.ts`'s constants of the same
name, used only to grey out buttons client-side; the backend re-checks on
every cancel/retry call and is the actual authority, so drift here is a
stale-button-state bug at worst, not a correctness bug.

## Manual verification against the real API

The compose stack (`nest`, `postgres`, `redis`, `parser`) was already running
in this working tree when this phase was implemented, but the running `nest`
container was built **before** this phase started (`services/downloader`'s
`/api/jobs` and `/internal/cookie/status` both 404'd against it) and two
other agents were editing `services/downloader` concurrently — rebuilding or
restarting that container was out of scope (shared stack, explicitly not to
be restarted). So:

- **Verified**: `docker build` of `services/web/Dockerfile` succeeds end to
  end (including `tsc --noEmit` + `vite build` inside the image); the built
  `nginx` image serves `index.html` on port 80 (`docker run` + `curl`
  checked manually). `pnpm dev` boots the Vite dev server cleanly.
- **Verified via component/integration tests with fixtures mirroring the
  real API shapes** (not live curl): job creation → appears in list on next
  poll tick, indeterminate enumerate bar while `total_videos` is `null`,
  `340/340` + "0 video mới" on a fixture shaped like a real F7 re-run,
  cookie badge red/amber/green, cancel/retry buttons calling the right
  endpoint with the right id, polling surviving a failed tick.
- **NOT verified live end-to-end** (would need a rebuilt `nest` container
  and a real Douyin profile / cookie): "xoá key cookie trong Redis → badge
  đỏ trong ≤2s" and "cookie push 40′ trước → badge vàng" against the actual
  Redis instance, and a real `340/340` re-run against actual enumerate/
  download lanes. The badge's polling + state logic is unit/integration
  tested against the exact `CookieStatus` shape `cookie.service.ts` returns,
  so the remaining risk is narrow (wiring, not logic), but it is still
  unverified live.

## Deploy — `web` service for `docker-compose.yml` (not added by this phase)

Phase-08's Todo includes "Compose: thêm service `web`", but
`docker-compose.yml` is a file shared by three agents working in this tree
concurrently and is out of this package's ownership scope. Add this block
under `services:` (and see the note below it):

```yaml
  web:
    build:
      context: .
      dockerfile: services/web/Dockerfile
      args:
        # Vite inlines these into the built JS at BUILD time (see the
        # Dockerfile's comment) -- they must be build args, not this
        # service's `environment:`, which a static nginx container never
        # reads.
        VITE_API_BASE_URL: ""   # same origin; nginx proxies /api and /internal
        VITE_API_KEY: ${API_KEY}
    ports:
      - "127.0.0.1:5173:80"
    depends_on:
      nest:
        condition: service_healthy
```

**Security note for whoever wires this in:** `VITE_API_KEY` ends up in the
built JS bundle in plaintext — any browser loading the page can read it via
devtools/view-source, and `docker build --build-arg` also leaves it
inspectable in the image's build history unless BuildKit secret mounts are
used instead. This matches the plan's own framing of this UI as "Internal
tool 1–3 người" (phase-08-web-ui.md Risks table) rather than a
public-facing app, and the alternative (a proxy backend that injects the
header server-side) is out of scope for this phase, but it should be a
conscious tradeoff for whoever deploys this beyond a trusted LAN, not a
surprise.
