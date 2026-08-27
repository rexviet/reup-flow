---
name: phase-05-uncovered-guards
description: Request-pacing guards in the enumerate lane that no phase-05 test or mutation reaches — re-check when phase 6/7 touch pacing
metadata:
  type: project
---

Two pacing guards in `src/lanes/enumerate.lane.ts` survive removal with the whole suite
green (verified 2026-08-25 by mutation, tree restored):

- **the jitter** in `pause()` — replacing the drawn delay with a constant keeps
  `test/lanes/enumerate.lane` (30/30) and `test/lanes/enumerate-global-concurrency` (3/3)
  green. See shape #3 in [[false-greens-reup-flow]].
- **the `await this.pause(signal)` on the HTTP-400 retry path** — deleting it lets the
  lane fire `MAX_CONSECUTIVE_REJECTIONS` requests back-to-back with no delay, and
  `test/lanes/enumerate.lane` + `test/lanes/cookie-recovery` stay green (33/33).

**Why:** F2's whole point is that Douyin rate-limits by IP, so an unpaced burst is the
failure mode the phase exists to prevent, and `test/mutations/phase-05.sh` (23 mutations)
covers only the *placement* of the between-pages sleep, not its shape or the retry path.

**How to apply:** when phase 6/7 add pacing (download lanes, F4 probe), assert gaps
*between* draws rather than bounds on one draw, and give every retry path its own gap
assertion. Do not trust "the mutation suite is green" for pacing.
