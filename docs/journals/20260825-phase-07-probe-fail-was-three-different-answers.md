# Phase 7: "Probe Fail" Was Three Different Answers Wearing One Word

**Date**: 2026-08-25
**Severity**: Caught pre-commit; the `markSkipped` race would have been High and silent
**Component**: F4 probe · retry/cancel · mutation runner · CI
**Status**: Resolved, with one gap handed to phase 10

## What Happened

Phase 7 closes M2. It is the phase where the parser's `"An error occurred."` — a static string that four different causes all collapse into — has to be turned back into an actionable diagnosis without patching upstream. Plus the full recovery loop, per-phase rate-limit tests, and the mutation suite finally becoming a CI gate instead of seven scripts someone remembers to run.

669 tests green. All seven harnesses in one run: 135 must-fail mutations red, 15 controls green, zero empty applications, and the compose stack rebuilt so the live smoke could actually be done — the thing phases 8 and 9 were blocked on.

## The Spec Said Two Outcomes. The Spec's Own Table Said Five.

§7.1 specified: probe fails → `blocked`, probe OK → `failed`. Clean, and wrong in a way that is easy to miss, because the table three lines above it lists **four** causes with **four** remediations:

| Cause | What the operator should do |
|---|---|
| Cookie banned | push a new cookie |
| `a_bogus` stale | pull upstream |
| Profile private/deleted | tell the user, stop |
| Parser cannot reach Douyin | wait, retry |

"Probe fails" merges three of those into one word. An operator reading `blocked` cannot tell whether to re-login, rebuild the parser, or check the network — and `blocked` for a stale `a_bogus` sends them to do the one thing that will not help.

So `probeCookie` returns **five** verdicts, each carrying its own reason, and the fixtures construct all five. That was not gold-plating: §5's ninth rule (written one phase earlier, from the extension's CORS blindness) says directly that if the fake parser cannot build all four causes, the F4 test proves nothing. A two-valued probe tested against a two-valued fake would have looked complete and measured nothing.

## Four Bugs, None With an Invariant Watching It

Phase 6's lesson was that a mutation harness grades the guards you wrote and is silent about the guard you never wrote. Phase 7's review found four more of exactly that shape:

**1. `markSkipped` could strand a video under a *live* job.** Cancel a job → the lane has already claimed video V → a new job is created, and its revival sweep sees V as `downloading`, not `skipped`, so it leaves it alone → the old lane then writes `skipped`. `claimOne` never looks at `skipped`, and `skipped` counts as settled, so the **new** job reaches `completed` with V missing and every counter in agreement.

That is the same failure family as phase 6's 0-byte file, and the third time this project has produced it: the damage is invisible precisely because all the numbers agree. Fixed with a `NOT EXISTS (live job)` predicate inside the `WHERE`, atomic with the write — not a check in the service layer, which a future code path could simply not call. Same reasoning the old plan used for putting `voice_consent_at` in the claim query.

**2. `park`/`giveUp` threw away the write result.** The probe is a 30-second call that does not renew the lease, so the reaper can legitimately take the job mid-probe. The write is gated on `status='enumerating'`, lands on nothing, and phase 5's code then logged `job X is BLOCKED` about a job that was already `pending` under someone else's claim. A log line asserting an outcome it never checked — the same defect shape as phase 6's "released … on shutdown".

**3. `ProbeHealthCheck` held bootstrap for the parser's full 30s timeout,** delaying `/health` and, through `depends_on: service_healthy`, the web container. A check deliberately built to be non-fatal was handing most of that harm back through a different door.

**4. `ProbeHealthCheck.done` could reject into nothing** — an unhandled rejection, i.e. a process crash caused by a diagnostic whose entire design goal was to not be fatal.

## The Harness Caught Its Own Author Three Times

Best evidence the gate works:

- One new test could not go red: it observed only DB state, which was identical with and without the fix. The **log** is the observable for that behaviour, so the log became the assertion.
- Two mutations were caught by **hanging** rather than failing, because a `beforeAll` assertion threw before `close` was assigned, leaking the Nest app. Rule six exists for exactly this and correctly refused to fold a hang into a generic red.
- One phase-7 mutation reported `matched nothing` **inside phase 7's own run**, because fix #4 moved the line its anchor pointed at. That is precisely the phase-04 staleness shape — except caught in the same run instead of surviving five phases undetected.

## The Deliberate Deviation

The risk register said `PROBE_SEC_USER_ID` pointing at a dead profile should **fail fast** at boot. It does not. It logs loudly at `error`, and boots.

Reason: fail-fast here takes the whole service down every time the network blinks, and the harm it prevents — one confusing `blocked` diagnosis — is smaller than the harm it causes. Rationale is in `probe-health.ts` next to the code, not only here. Verified live: this machine cannot reach Douyin, boot printed `F4 PROBE TARGET IS NOT USABLE …`, and the service came up.

## The Gap Nothing Could Have Seen

`docker compose up` on a clean machine still does not work. **Nothing applies migrations.** Compose's Postgres boots empty, so every DB-touching route 500s until someone runs `migrations/260824-init-schema.sql` by hand.

The test suite cannot see this, structurally: every suite creates its own schema. Six phases of integration tests against a real Postgres, and not one of them exercises the path a new operator takes. It was found by rebuilding the stack and using it — the same way phase 9's CORS problem was found by asking what a real browser would do.

That is the ninth rule pointing at itself: the instrument covers the boundary it was built to cover, and "clean machine" was never inside it. Phase 10 owns the fix; the whole-plan DoD line is now marked not-met with the reason, rather than quietly unticked.

## Also Recorded

- The CI mutation gate takes ~25 minutes. Accepted deliberately: a slow gate beats a gate nobody runs. `scripts/run-mutations.sh 04 07` splits it.
- `EXPECT` in the runner declares each harness's mutation and control counts up front, and a count mismatch fails the build. That is the direct answer to `phase-04.sh` losing two mutations for five phases while every run reported clean.
- `phase-07.sh`'s `INT`/`TERM` trap restores **and exits**; phases 4–6 restore and then continue to the next mutation, so a `kill` there does not actually stop those harnesses. Inconsistency left as-is, noted here.
- One intermittent failure in `test/lanes/download.lane.spec.ts`, once, in the run right after the mutation suite under load. Not reproduced in three full-suite and six targeted runs, message not captured. Same shape seen once earlier in `download-commit-race.spec.ts`. Watch it in CI rather than pretending it did not happen.
