# Phase 3: The Harness That Grades The Guards Was Itself Wrong — In Both Directions

**Date**: 2026-08-25
**Severity**: Caught pre-commit; three of these would have been High in production
**Component**: HTTP API layer / secret redaction / mutation harness
**Status**: Resolved, two items deliberately carried

## What Happened

Phase 3 shipped as `decea9f`: jobs/videos/cookie controllers, an `X-Api-Key` guard on every route, a process-wide redaction layer, and two-layer enforcement of "the HTTP layer never calls a lane". 331 tests green, lint clean, build clean, gitleaks clean. A mutation suite of 20 entries reported every mutation caught.

Then an adversarial review found three ways a live session cookie could still leave the process, two tests that could not fail for the reason they claimed, and one risk-register row that this phase was assigned and never implemented. All of it sat underneath a green suite.

But the entry is not really about those. It is about what happened when the mutation harness — the thing whose entire job is to prove the guards work — turned out to have two bugs of its own, and reported confidence in both directions while doing it.

## The Pattern

Phases 0, 1 and 2 each ended with the same lesson written a different way: *green is evidence the suite ran, not evidence the invariant holds.* §5 of the plan encodes it as enforcement rules — print the diff a mutation actually applied, treat an empty match as "never ran".

Phase 3 found the half of that rule nobody had written down. A mutation has two halves — **the code really changed** and **tests really ran** — and §5 only guarded the first.

## Technical Details

### 1. A Multi-Word Test Pattern Matched Zero Tests, And That Read As Success

**Status**: 17 mutations reported "caught". **Actual**: five of them ran no tests at all.

The runner did `pnpm test -- "$pattern"`. For a single-word pattern that is fine. For `test/cookie/ test/redaction/cookie-leak` the shell passes one quoted argument, jest treats the whole string as **one** path regex, matches nothing, prints `No tests found`, and exits 1.

Exit 1 is exactly what a caught mutation looks like.

So five must-fail mutations reported `OK: caught` having executed nothing. And the same bug ran the other way: two must-stay-green **controls** reported `FAIL: control went red` against code that was completely correct. Wrong in both directions, and plausible in both — a "caught" mutation is what you hope for, and a red control is a finding you go investigate.

I went and investigated. That is how the next one surfaced.

**Fix**: word-split the pattern, and treat `No tests found` as a **hard error** rather than as a signal in either direction. A mutation is evidence only when the code changed *and* a non-zero number of tests ran.

### 2. `git checkout --` Silently Restores Nothing On Untracked Files

**Status**: mutation restored, suite green. **Actual**: the mutation stayed applied for the rest of the run.

One mutation needed to break two files at once (inject a leak *and* disable the defence). The helper backed up and verified one file; for the second I wrote `git checkout -- "$STDIO" 2>/dev/null || true`.

Every source file in a brand-new phase is untracked. `git checkout --` on an untracked path does nothing and exits non-zero, swallowed by the `|| true`. The stdio redaction patch stayed no-op'd for every subsequent mutation, so each later cookie assertion was measured against a crippled build — and still reported "caught", because a crippled build fails a lot of things.

The tell was subtle: a spec that had passed twenty minutes earlier now failed with no relevant change. Chasing *that* is what exposed both bugs.

**Fix**: one restore mechanism, applied to every listed file, and verified by diff. Plus: a multi-file mutation must change **every** file it names — "at least one changed" let this mutation apply half of itself and still pass its own empty-diff check.

### 3. The Must-Stay-Green Control Found A Real Ordering Defect

Phase 2 added §5's second rule: when more than one mechanism can hold an invariant, add a control that removes the weaker one and **must stay green**, because a red mutation only proves the touched line matters *somehow*.

With the harness fixed, one control stayed red. `CookieService.push` registered the cookie with the exact-value redactor as its **last statement** — the natural place, as the final step of a successful push. That leaves the entire body of the method a window in which the value is live and not yet redactable, and the likeliest thing to write during that window is the failure path: ioredis routinely embeds the argument it failed on, and the caller writes that to stderr.

No passing test caught it. Every happy-path assertion is satisfied by registering last. The control caught it, in a direction the rule was not designed for: it did not tell us *which mechanism* holds the invariant, it told us the *ordering inside a method* was wrong.

Now `registerSecret` runs immediately after platform validation, and a test forces the Redis write to throw and asserts the cookie is scrubbed anyway.

### 4. Three Redaction Escapes Under A Green Suite

- **`Uint8Array` chunks bypassed the stdio patch entirely.** `Buffer.isBuffer()` is false for a plain `Uint8Array`, so the fall-through forwarded them raw. Anything using `TextEncoder().encode(...)` or piping a web stream to stdout leaked verbatim.
- **The field pattern required a literal `=`**, so it missed `{"sessionid":"..."}` — which is precisely the shape `redact.logger.ts`'s own `JSON.stringify` branch produces for an object argument. The test that looked like it covered this did not: its fixture nested `field=value` *inside* a JSON string value, so the old pattern matched for the wrong reason.
- **A secret split across two `write()` calls leaked** — and when the split landed mid-value, the head was redacted and the tail was not, producing output that *looked* redacted while leaking. A partial match is worse than no match, because it reassures.

The fix for the third exploits one property: none of these secrets ever contains a newline. So a per-stream buffer can flush everything up to the last `\n` immediately and hold only the trailing partial line — closing the hole with no output delay for ordinary line-oriented logging.

One further note: the first version of the widened pattern normalised every hit to `field=[redacted]`, turning `{"sessionid":"..."}` into `{"sessionid=[redacted]}` — no longer parseable JSON. That removes the secret and destroys the record, trading a leak for a different kind of blindness. The replacement now preserves the original delimiters.

### 5. Two Tests That Could Not Fail

- The cookie-leak spec's "never returns the cookie in an API response body" read its evidence out of the child process's **already-redacted stdout**. It was not testing the HTTP layer; it was re-testing the stdio patch that three other assertions already covered. Now the harness writes raw response bodies to a *file*, which the patch does not touch.
- Three `not.toHaveProperty('lease_expires_at')` assertions guarded a key the code never emits — the Drizzle property is `leaseExpiresAt`. Spreading the ORM row into the response left all three green. Now asserted against the serialised body with `/lease.?expires/i`.

### 6. The Risk Row This Phase Was Assigned And Did Not Do

`plan.md` §6: *"Cookie vào `error` của job rồi ra API response — redact phủ cả đường ghi `jobs.error`"*. There was no redaction on either the write or the read path, and both `jobs.error` and `videos.last_error` are served straight to API clients. Nothing was broken yet only because phase 3 does not write those columns — phase 6/7 will, with externally-influenced text. Now redacted on both paths.

Also found: `POST /api/jobs` accepted any URL and handed it to a parser that **follows** links. The parser container deliberately has no `ports:` precisely because it is an unauthenticated scraper; the endpoint handed that reachability back out to any authenticated caller. Host-allowlisted now.

## What I'd Do Differently

Check the harness before trusting its verdict. I spent real time treating a red control as a code defect when the first run's controls were red for a shell-quoting reason — and the genuinely red control only surfaced *after* the harness was fixed. Both conclusions were available from the same output; only one was correct, and nothing in the output distinguished them.

The generalisable form: **a tool that reports confidence fails by reporting confidence.** The mutation harness grades every other guard in this repo, so a bug in it is invisible by construction. It needs the same treatment we give the guards — break it on purpose and require it to notice.

## Follow-ups

- Two DoD lines deliberately left unticked: the architecture test's real form (a raw-SQL row processed by an actual running lane) needs a lane, and moved to phase 4; the short-link/full-URL equivalence uses a `FakeParser` returning a constant, with real coverage only in the opt-in `pnpm test:integration`.
- Chunk-boundary redaction still flushes the head of any single line exceeding 64 KiB with no newline, accepting a split-secret risk over unbounded memory. Documented at the code.
- The secret registry is capped at 32 entries (FIFO), so a very old cookie in a very delayed log line is no longer scrubbed. Chosen over unbounded growth and a per-write cost that degrades with uptime.
