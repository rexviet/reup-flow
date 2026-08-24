# Phase 0: The Rule Was Written Down. We Broke It Five Times Anyway.

**Date**: 2026-08-25 09:40
**Severity**: Caught pre-commit; would have been High to Critical in production
**Component**: Downloader service skeleton / DB claim spike / cookie-swap spike / bootstrap / container hardening
**Status**: Resolved with Follow-ups

## What Happened

Phase 0 of the douyin-downloader plan shipped as commit `0b09d8c`: a skeleton service plus two hard gates (Drizzle `SKIP LOCKED` claim, live cookie-swap against the real fork). Both gates PASSED. `plan.md` §0 now carries an inviolable principle written directly from phase 1's journal: don't trust a green check you haven't mutated. Phase 0 had a mandatory mutation table in its own DoD for exactly this reason.

Phase 0's entire stated purpose — spike gate whose only job is to not repeat phase 1's false greens — produced five new ones. All five were caught by adversarial review before the commit landed. All five are now mutation-proven. That's the whole entry.

## The Brutal Truth

We wrote the lesson down. We built a mutation table into the DoD to enforce it. And the discipline still didn't transfer to the actual work — it took someone sitting down and asking, test by test, "what's the dumbest wrong implementation that still makes this green?" That's not a process failure you fix by writing another paragraph in a plan file. Knowing the failure mode and avoiding the failure mode are different skills, and apparently the gap between them is wide enough to swallow two entire phases in a row.

The redeeming part: the review process phase 1 demanded actually worked here, on schedule, before the damage shipped. That's the only reason this entry isn't titled "production incident." Small mercy, still a mercy.

## Technical Details

**1–2. SKIP LOCKED tests named for concurrency, passing with the lock removed.** `Promise.all([dbA.transaction(...), dbB.transaction(...)])` isn't concurrent — each leg is a client-side BEGIN/UPDATE/COMMIT round-trip, and A finished before B issued its SELECT. Tell: 10 "concurrent" claimers completed in ~20ms. Adding a barrier so all claimers park before claiming was *still not enough* — Postgres's `LockRows` node retries the next candidate after a blocked lock releases and its recheck fails, silently self-healing a missing `SKIP LOCKED` as long as commits are fast. Fix: hold every winning claim's transaction open until all contenders settle, so a blocked claimer has nothing to wake for and must hit `statement_timeout`.

**3. `API_KEY` secrecy test used a valid key.** Zod raised no issue on that path, so no realistic leak formatter could ever surface it in the message. Re-applying the exact regression its own docstring warned against: 9/9 green.

**4. Two required env vars silently downgradable to optional-with-`""`-default, 16/16 still green.** DoD said "missing var ⇒ crash at startup," proven for 3 of 5 required vars. `API_KEY` becomes phase 3's `X-Api-Key` credential — a silent `""` default is `if (header === env.API_KEY)` with `env.API_KEY === ""` and an empty header. Auth-bypass primitive, caught before it existed anywhere but a test file.

**5. Cookie-swap "5 concurrent requests mid-flight" was neither.** `callParser` only *starts* a fetch; a Redis SET on an already-connected local socket completes before the TLS handshake does. The swap landed before request 0 sent headers. Its own printed output — "all five saw B" — disproved its own comment. This one stung specifically because phase 1's journal named this exact gap ("an integration test with two event loops in a real parser lifecycle") as the thing that still doesn't exist, and this step was sold as the closest approximation.

**6. Anti-restart guard watched the wrong process.** It diffed `docker inspect` `.State.Pid` / `.State.StartedAt` — container PID 1, `/bin/sh ./start.sh`. The fork's `start.py` runs `uvicorn.run(reload=True)`, so the process actually serving requests is a `multiprocessing-fork` worker three levels down. Reviewer killed that worker: both watched fields stayed byte-identical, `.State.Running` stayed `true`, app was dead. The one failure mode the guard existed for was invisible to it.

**Two harness bugs found fixing #6, both would have been false greens themselves:**
- SIGKILL-based worker restart didn't work at all — `StatReload` has no child-health-check or auto-heal, so killing the worker just leaves the app permanently dead. Replaced with `touch /app/start.py`, a real watched file that drives StatReload's own restart path. Verified: worker PID 21 → 58.
- The worker-identity helper matched `"multiprocessing-fork"` as a substring of `/proc/<pid>/cmdline`, and self-matched the `docker exec python3 -c "<script>"` invocation doing the matching, because that script's own source contains the string. Every call reported a "new" worker even on an unmutated run.

**Other things quietly broken, no test involved:** bootstrap had no positive control — `bootstrap-failure.spec.ts` only checked "bad env exits non-zero," so a `main.ts` that never boots at all passes every test (verified: unconditional throw injected, 11/11 still green). `pnpm lint` exited 2 — no config, never ran; phase 3 plans to enforce an HTTP-layer import boundary with ESLint, on a linter that doesn't execute. The parser's cookie-capture log (`captured.log`) was on its way into git — `.gitignore`'s `/data/` is root-anchored, didn't cover it. `nest` received the entire `.env`, including a live production Douyin session cookie and the Postgres password, neither of which it reads. Downloader container ran as root next to an unauthenticated scraper. Toolchain split: no `packageManager` field, host on pnpm 9.15.4, corepack fetching 11.23.0 inside the image — surfaced as a build failure because pnpm 11 ships a `minimumReleaseAge` policy that rejected three transitive deps published that same day. Pinning back to 9 would have silently deleted that protection; pinned forward to 11 and regenerated the lockfile instead.

## What We Tried

1. Trusted the mutation table in `plan.md` §0 to be self-enforcing because it existed → it didn't self-enforce anything; it's a checklist, not a mechanism.
2. Read "SKIP LOCKED test passes" as proof of concurrency → `Promise.all` on sequential client round-trips is not concurrency, and nothing in the assertion checked wall-clock overlap.
3. Read "suite went red on mutation" as proof the test caught it → dropping `skipLocked` turned 2 of the 4 tests in that file red (the generated-SQL assertion and the one genuinely-overlapping test), while the two tests *named for concurrency* stayed green. The suite total said RED and the specific claim under review was not actually defended. Checking suite totals instead of per-test results almost let that through as "handled."

## Root Cause Analysis

Same defect class as phase 1, restated: a check that has never been asked to fail is not a check, it's a formality that happens to compile. What's different — and worse — this time is that the rule was already written down, in force, with a mandatory mutation table attached to the DoD, and it still didn't prevent the same shape of bug from being authored fresh five separate times in one phase. Mutation testing worked as a *detection* mechanism. It did nothing as a *prevention* mechanism, because writing a test named `concurrent_claim` doesn't make a developer's mental model of what "concurrent" means in a `Promise.all` correct at the moment of writing it. The gate was mutation-tested; the *phase 0 CI jobs themselves* were not — three mutation steps existed for the fork's gates, zero for phase 0's own gates, until this review manually ran them. The mutation discipline phase 1 earned had not actually propagated into automation; it was living in someone's head and in a markdown table, not in a pipeline.

## Lessons Learned

1. **Writing the rule down does not install the rule.** `plan.md` §0 has been an inviolable principle since phase 1's journal, with a mutation table required in every DoD since. It still took a human adversarially interrogating five separate checks to catch five separate false greens in one phase. Process documents change what's checked at review time; they don't change what gets written at authoring time.
2. **Check per-test results, not suite totals, when validating a mutation.** "The suite went red" is a different, weaker claim than "the specific test I mutated against went red." Two SKIP LOCKED mutations were briefly logged as caught because the file's suite failed, while the targeted tests were still green underneath the noise.
3. **"Concurrent" in a test name is a claim, not a property.** If the implementation is `Promise.all` over async functions that each do their own connect/begin/commit round trip, verify overlap with a barrier and a timing assertion, not just parallel dispatch syntax.
4. **A `docker inspect` state check is not a process-identity check** when the entrypoint spawns children. If the framework can respawn workers under a stable container PID (uvicorn `reload=True`, gunicorn, any supervisor), the identity that matters is the worker's, and PID/StartedAt on the container tells you nothing about it.
5. **Mutation steps belong in CI for every gate that ships, not just the ones a previous incident singled out.** The fork had three mutation-guarded CI gates from phase 1. Phase 0's own two gates had none until this review added them by hand.

## Next Steps

- [x] Add mutation checks as permanent CI steps for `services/downloader`. Done in this phase: the `downloader` job now applies three mutations (drop `skipLocked`; drop the row lock entirely; add `ports:` to `parser`) and fails if any of them leaves the suite green, verifying each diff actually applied first.
- [ ] The cookie-swap gate's three mutation scripts (`mutations/0{1,2,3}-*.sh`) exist and assert correctly, but CI runs only `run.sh` — the mutations themselves are still local-only proofs. Fold them into the weekly `downloader-cookie-swap-gate` job — owner: whoever picks up phase 10's upstream-pull work.
- [ ] Extend the anti-restart guard's process-identity check to any future service in this repo that wraps a dev server with hot-reload; grep for `reload=True` / `--reload` in new Dockerfiles as part of review, since PID-1-only checks will silently mis-target every time.
- [x] Fix `pnpm lint` (no config, never ran). Done in this phase: `.eslintrc.json` added and mutation-verified by planting two unused vars and confirming 2 errors. Phase 3 can now land its `no-restricted-imports` boundary on a linter that actually executes — that rule would otherwise have been the sixth false green.
- [ ] Rotate whatever `.env` was in scope during this phase's local runs — `nest` held a live Douyin session cookie and the Postgres password it never reads; treat both as burned.
- [ ] Confirm the `.gitignore` fix for `captured.log` actually keeps it out (it is not root-anchored the way `/data/` assumed) — reverify with `git status --ignored` after the next parser run, not by reading the pattern.

**Status:** DONE | DONE_WITH_CONCERNS
**Summary:** Phase 0 shipped with both gates genuinely passing and genuinely mutation-proven — but only after adversarial review caught five fresh false greens of the exact class phase 1's journal warned about, in a phase whose entire job was to not do that.

**Concerns/Blockers:**

- The mutation table in `plan.md` §0 is a review-time control, not an authoring-time one. Nothing currently stops phase 1 (claim loop) from reintroducing a sixth variant of this defect class; the only defense that has actually worked twice is a human going line by line asking "what's the simplest wrong version that still passes." That doesn't scale past someone remembering to do it.
- CI now runs mutation steps for phase 0's two gates, added by hand during this review. If a future phase adds a new gate without its own mutation step in CI, it inherits this exact gap silently.
