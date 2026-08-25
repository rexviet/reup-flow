# Phase 4: The Shutdown Hook Ran Too Late To Do The One Thing It Existed For

**Date**: 2026-08-25
**Severity**: Caught pre-commit; the first item would have been High in production
**Component**: Lane framework / graceful shutdown / mutation harness
**Status**: Resolved

## What Happened

Phase 4 built the lane framework: `LaneRunner` (a long-lived self-scheduling async loop, Approach 1 from the solution design), a `LaneSupervisor` that starts and stops lanes, and the reaper — four 60-second sweeps that recover orphaned videos (F3), orphaned jobs (F5), un-finalized jobs, and abandoned `.tmp` files.

Every in-process test of graceful shutdown passed. `stop()` aborted the loop, drained the tick in flight, and released the held lease; the supervisor stopped every lane concurrently and swallowed individual failures. Twenty-two tests across two files, all green.

Then the child-process test ran, sent a real `SIGTERM` at a real process, and looked at the database row afterwards. The lease had not been released.

## The One That Mattered

Nest tears down in a fixed order:

```
every onModuleDestroy  →  every beforeApplicationShutdown  →  every onApplicationShutdown
```

`DbModule` closed the pg pool in `onModuleDestroy` — the natural place, and where it had lived since phase 1. `LaneSupervisor` released leases in `onApplicationShutdown`. So by the time a lane tried to hand its claim back, the pool was gone:

```
lane holding: failed to release video sigterm-video-1 on shutdown:
  Error: Failed query: update "videos" set "status" = CASE WHEN ...
```

The consequence is not a degraded shutdown. It is **no release at all**: the lease holds for its full `LEASE_SECONDS` (300 by default), and the restarted process sits next to work it is not permitted to claim for five minutes. That is precisely the symptom §4.2 was written to prevent, produced by the code written to prevent it.

Fix: the supervisor moved to `beforeApplicationShutdown`, and `DbModule` / `ParserModule` / `RedisModule` moved their teardown to `onApplicationShutdown`. Releasing a lease is a write, so every connection a lane needs must survive into the middle phase and may only close in the last.

### Why every in-process test missed it

Because they all call the hook, or `app.close()`, and then assert that *release was called*. None of them asked whether it **succeeded**. A `jest.fn()` that was invoked looks identical whether the query behind it worked or threw — and the supervisor swallows release failures on purpose, so nothing propagated.

The child-process test asks a different question: not "did we call release" but "is the row `pending` now". That question cannot be answered by a spy, and it is the only question the DoD actually cares about.

This is the phase-1 lesson in new clothes. Then it was a `print()` that a logger-watching test could not see. Here it is a failed UPDATE that a call-watching test could not see. **Observe the outcome, not the invocation.**

## Three More Real Bugs, Found By Tests

**A lane reporting `'worked'` forever starves the event loop.** "Do not sleep after a tick that did work" is correct — a lane with a queue in front of it must come straight back. But a `while` loop whose only `await` resolves inside the microtask queue never returns to the macrotask queue at all: no timers, no sockets, no signal handlers. The API and the lanes share one process by design, so a starving lane means `/health` stops answering, Nest's SIGTERM handler never fires, and the container is SIGKILLed with its lease still held.

Real `runOnce` implementations await Postgres and yield anyway, which is exactly what makes this the kind of bug that surfaces once, under some cheap in-memory path, in production. Fixed with one `setImmediate` per worked tick.

It was found because the test file **hung instead of failing**. That symptom recurs below.

**`backoffMs` captured its floor before a subclass could set it.** `private backoffMs = this.errorBackoffMs` is a field initialiser, so it runs during the *base* constructor and captured the base default of 1000ms — every value a subclass or test set afterwards was silently ignored. Fixed by deriving the delay from a failure counter instead of storing it, which removes the initialisation-order question rather than answering it.

**The last line of defence could itself fall over.** The loop has an outer catch whose only job is to *record* a death that the inner catch somehow failed to survive. The only way to reach it is for the inner catch's own error handling to throw — i.e. for the logger to be broken — and the outer catch then called that same logger, threw again, and turned a recorded death into an unhandled rejection. A handler that exists to make failure visible must not be able to fail.

## Two More, Found By Reading Rather Than By Testing

Neither of these had a failing test. Both came out of an adversarial pass over the finished code, which is worth recording because the suite was green and the mutation run was clean at the time.

**`releaseHeld` was memoised for the lane's lifetime, not for the claim.** The memo exists so two concurrent `stop()` calls cannot both release — a second release could hand back a row a different worker has since claimed. But once a lane had released anything, the resolved promise stayed in place forever, so a **restarted** lane's next claim was never handed back at all. Nothing restarts a lane inside one process today, which is exactly why this would have sat there until something did. Fixed by clearing the memo in `hold()`, which scopes it to the claim — the scope it always meant.

**`isStale()` made a false claim about a slow starter.** It answered true for any lane that had not ticked yet, whatever the clock said. So sixty seconds after boot, a lane with a slow first tick — an enumerate lane waiting on a cold parser — produced `has not ticked in over 300000ms (last tick: never)`. That sentence is simply false, and a false alert is worse than no alert because it teaches an operator to ignore the real one. Now: never *started* is stale immediately (a lane the supervisor skipped is a genuine problem with nothing to wait for), started-but-not-yet-ticked becomes stale only once the window has actually elapsed.

Both were found by asking "what does this produce in the case nobody has hit yet" rather than by running anything. Worth doing deliberately at the end of a phase; the mutation suite cannot find a bug in a code path no test exercises.

## And Two More In The Harness

The mutation harness is the thing that grades every other guard, so a bug in it is invisible by construction: it breaks by reporting confidence. Phase 3 found two. Phase 4 found two more, and both are now rules in plan §5.

### Rule 5 — a harness that is *killed* must not poison the tree

Rules 1–4 all quietly assume the script reaches its own `restore` call. A watchdog `pkill` landed between `_apply` and `restore`, and the mutation stayed baked into the working tree. The file was **untracked** — in a new phase, *every* file is — so `git checkout` had nothing to offer even if anyone had reached for it.

The next run measured a crippled build. Its second mutation reported `matched nothing` (the shape it expected was already mutated), and every result after that was meaningless. Two of the four bugs above were, briefly, "found" this way — I spent a while repairing files by hand before realising the harness had done it.

Fix: snapshot every target file before anything runs, `trap ... EXIT INT TERM` to restore, and **print that a restore was needed**. A silent restore is indistinguishable from no restore. It earned its place within the hour: the next kill left the tree clean by itself.

### Rule 6 — a mutation can *hang* instead of failing

The starvation mutation does not make tests red. It makes the event loop stop turning — so jest's own per-test timeout cannot fire either, and the suite stalls forever at that mutation, reporting nothing about it or anything after it.

Fixed at both ends:

- The harness bounds each jest run and reports a hang as **its own outcome**, never folded into "red". A hang is a legitimate catch for a must-fail mutation and a *failure* for a control. Collapsing it into a generic red is how "Postgres is down" would come to look like sixteen successful mutations.
- The test was rewritten so it *can* go red: the fake lane caps how long it will keep reporting `'worked'` (forcing an eventual idle sleep, which lets the event loop drain and the assertion run), and the probe is a `setImmediate` rather than a `setTimeout`, because a starved event loop starves the thing measuring it too.

**A mutation that cannot be caught is a guard that is not guarded.** Six of these harness rules now exist, one per phase, which is itself the finding: the harness has been wrong more often than any single guard it grades.

## What Shipped

20 mutations (19 caught by tests, 1 by ESLint), all caught. 2 must-stay-green controls, both green. 443 tests across 42 suites, six consecutive clean runs, lint clean, gitleaks clean.

The two controls are the interesting ones, and they exist because "an orphaned video comes back" is held by **two** mechanisms — the reaper (eventually) and the shutdown release (immediately). A red mutation on either proves only that the line matters somehow, not which mechanism the test measures. So: remove the reaper's video sweep and require the SIGTERM immediacy test to stay green; remove the shutdown release and require the reaper's own tests to stay green. Without those, "restart picks it up *immediately*, not in five minutes" would have been unproven in both directions.

## Carried Forward

- **Phase 6 owes a conditional release.** `HeldWork.release` is called after the drain, and the drain can time out, so in the worst case it runs while a tick is still in flight. Today that window is closed by arithmetic — the drain timeout is seconds, a reaper cannot touch the row for 300 — but arithmetic is not a guard. Phase 6 already owes a lease check before its `rename`; the same check belongs on the release.
- **`removeStaleTemp`'s in-flight registry is process-local.** It is complete for today, because the API, the lanes and the reaper share one process. The moment lanes move to their own process — which `LANES_ENABLED` now makes cheap — the mtime cutoff becomes the only guard. That is why the cutoff is a real check and not a formality.
- **A worker-leak warning appears intermittently** on full-suite runs ("a worker process has failed to exit gracefully"). Ten consecutive runs are green and `--detectOpenHandles` reports nothing; noted rather than chased, because the class of thing it hints at is exactly what rule 6 is about.
