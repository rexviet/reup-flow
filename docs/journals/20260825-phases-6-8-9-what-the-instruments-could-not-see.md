# Phases 6, 8, 9: Three Lanes in Parallel, and Two Failures No Green Suite Could See

**Date**: 2026-08-25
**Severity**: Caught pre-commit; the phase-6 one would have been High and permanent in production
**Component**: Download lane · web UI · MV3 extension · mutation harness
**Status**: Resolved, with three deferrals named below

## What Happened

Three phases ran at once as three agents in one working tree: phase 6 (download lane, the M1 gate), phase 8 (web UI), phase 9 (Chrome extension). Phases 8 and 9 depend only on phase 3's API, which is what made them parallelisable — the plan said so back in §4 and it held up.

The parallelism itself was uneventful, which is the useful part. Each agent owned exactly one package, was forbidden from `git add -A`, and was kept out of every shared file — `plan.md`, `docker-compose.yml`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`. All three commits landed inside their boundary with nothing to reconcile; the shared-file sync was one commit afterwards. The cost of that discipline was three deferred items and a lockfile left dirty for twenty minutes. Worth it: three writers racing on one git index would have produced commits nobody could later attribute.

The interesting part is what the tests could not see.

## The One That Mattered: A 0-Byte File That Called Itself `done`

The download lane verifies a finished stream against `Content-Length` before committing:

```ts
if (bytes !== expectedBytes) throw new TruncatedDownloadError(...)
```

`Content-Length: 0` passes that check. Zero equals zero. The file then gets fsync'd, renamed into place, and marked `done`.

And `claimOne` never revisits a row that is `done`.

So the archive holds an unplayable file, forever, and **every number agrees**: `downloaded_count` is right, the file count on disk is right, no error is logged, nothing is `failed`, and the phase's own DoD line — "số file trên đĩa khớp `downloaded_count`" — passes. The DoD was written to catch missing files. This is a present file with nothing in it, which is a shape the DoD did not have a word for.

This is the fourth time in this project that the failure mode was *agreement between wrong numbers* rather than a red anything. It was found by the adversarial review pass, not by a test, and not by a mutation — there was no guard to mutate. A mutation harness scores the guards you thought to write; it is silent about the guard you never wrote. Now guarded in the commit hook (covering the missing-header case too) with a mutation of its own.

## The Second One: 48 Green Tests That Structurally Could Not See CORS

The extension's service worker `fetch`es the operator's backend URL. That origin was not in `host_permissions`. In a real Chrome it dies on CORS, on the very first push, every time.

Forty-eight tests passed. They had to: Jest's `fetch` stub does not enforce CORS. The test double was incapable of failing the way the real thing fails.

This is a harder case than phase 5's descending cursor. There, the fixture had the wrong *data shape*, and the fix was to make the fixture tell the truth. Here the fixture's data was fine — what was missing was an entire **failure direction the instrument cannot construct**. No amount of mutation testing reaches it, because mutations probe the code and the gap was in the measuring apparatus. It went into `plan.md` §5 as the ninth rule: before trusting a suite that covers a boundary going outward — network, browser permissions, filesystem, clock — ask whether the double has any way at all to fail like the real thing. If not, that case is unverified, and it must be written down as unverified rather than allowed to hide inside a count of 48.

## The Harness Was Wrong Again, in a New Way

Phase 3's lesson made `No tests found` a hard error. That covered one shape and not the neighbouring one: when `-t` matches nothing, jest does **not** print `0 total`. It prints

```
Tests: 7 skipped, 7 total
```

and exits 0. A guard searching for `0 total` is blind to that. Which means a `CONTROL:` block that gets renamed makes both must-stay-green controls unfalsifiable — and they report green, i.e. they report exactly the conclusion we were hoping for. The guard now demands positive evidence that some test actually passed or failed. Same commit fixed the hang path, which was killing the `pnpm` wrapper and orphaning jest's workers along with their Postgres connections.

Also worth recording: **two of phase 4's mutations had already gone stale before phase 6 started.** Phase 5 changed the `LANES` factory and the `finalizeSettled` WHERE clause without updating `phase-04.sh`, so it was exiting 1 with "matched nothing" — the failure mode rule one exists to catch, sitting in the repo unnoticed while five phases reported clean runs. Mutation scripts are source that goes stale like any other, and nothing was re-running them. All harnesses were re-run after the fix: 8 · 15 · 18 · 20+2 · 32+2 · 14+3.

## Four More Assertions That Could Not Go Red

The phase-6 review also found, and the agent fixed:

- `settle()` ran unguarded inside the failure `catch` and could replace the very error it was called to report — precisely the defect the neighbouring comment claimed to have closed.
- `release`/`markFailed` returning `false` was discarded, so the shutdown path logged "released … on shutdown" for a give-back that never landed. A log line asserting an outcome it never checked.
- A heartbeat beat could outlive its `clearInterval` and mark a row `lost` *after* a successful commit. `stop()` now awaits the beat in flight.
- `it.each([403, 410])` served 403 for both halves, and the integrity probe was a short-circuiting disjunction that certified the wrong status. The 410 case had never run.

## What Is Not Done

Three deferrals, stated plainly so they are not discovered as surprises:

1. **No scheme/host allow-list on `play_addr`.** The CDN client requests whatever URL the parser returns. Redirects are off, so the blast radius is a single GET, but a mangled or hostile parser response could aim an authenticated-network request anywhere and the body would land on disk as an `.mp4`. An `https:` + suffix allow-list is cheap; it needs a `127.0.0.1` exemption for every `FakeCdn` test. Phase 7 or 10.
2. **A cancelled job's `skipped` videos have no way back.** `cancelled` is not retryable and `resetFailedForProfile` only resets `failed`, so a later job on the same profile re-enumerates them, `upsertMany` correctly does nothing (F7), and they stay `skipped` forever. `TODO(phase-7)` is in `jobs.repository.ts`; phase 7 owns retry/cancel.
3. **Live verification is blocked on a stale container.** The running `nest` image predates the cookie module and 404s on `/internal/cookie/status`, so two of phase 8's DoD lines (badge red on cookie deletion, badge yellow at 40 minutes) and all of phase 9's browser-level DoD are unproven. Rebuilding was off-limits while phase 6 was driving integration tests through the same compose stack. This needs one rebuild and one manual pass in Chrome — the logic is unit- and mutation-tested; the wiring is not.

Also accepted, not deferred: `VITE_API_KEY` is inlined into the web bundle in plaintext. Inherent to a static frontend with no BFF, and this is an internal tool for one to three operators per the plan's own risk register. If that UI ever moves somewhere reachable, the answer is a backend-for-frontend holding the key, not a cleverer hiding place.

## What M1 Actually Means Now

Phase 6 closes the M1 gate: a whole profile downloads end to end, files match `downloaded_count`, a real `SIGKILL` mid-download resumes without re-fetching anything already `done`, F1 makes zero `fetchOneVideo` calls while the cache is warm and re-resolves on 403/410, cancellation leaves no `.tmp` behind. All of it against a real Postgres and a real HTTP fake CDN.

Against a *fake* CDN. The plan's whole-plan DoD still asks for 200+ videos from the real Douyin, and that line is still unticked. M1 means the machine is correct on the evidence we can construct. It does not yet mean it has met the internet.
