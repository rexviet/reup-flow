---
title: Companion editing and durable one-cue revoice
date: 2026-11-09
summary: Implemented phase 04 backend and companion; native Studio and live-provider acceptance remain open.
---

# Companion editing and durable one-cue revoice

## What happened

Implemented the loopback companion, review-only configuration, one-use launch sessions,
HEAD/CAS caption saves and projection recovery, a guarded host launcher, and a standalone
one-cue revoice workflow using the existing synthesis/provider/mixing paths.

Real ffmpeg and local Temporal tests, browser mixed playback, literal caption/SRT handling,
legacy replay and worker wiring passed. Browser checks found preview scaling/playback and
reload-harness issues; these were corrected. Host raqm checks ran successfully in the
Linux test image. A short-lived synthetic-fixture grant appeared in process-inspection
output and was invalidated immediately; no production credential was involved.

## Decision

Preserve unimported Studio edits and reject stale results. Bound queued reservations by
S3 CAS rather than eventually consistent Temporal visibility. Keep the native Studio
launcher fail-closed until its Host/Origin gate is resolved. Do not mark the phase accepted
from synthetic-provider tests, and do not activate review delivery or apply migration.

## Next steps

Complete native Studio security/close-reopen/override gates and the dedicated live smoke.
Then execute the separately scoped rollout phase. Evidence and operating instructions:
plans/reports/cook-261110-0620-hyperframes-phase-04.md and docs/review-companion.md.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
