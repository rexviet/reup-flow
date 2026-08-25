# Phase 5: The Guard From Phase 4 Assumed Douyin's Cursor Counted Upwards

**Date**: 2026-08-25
**Severity**: Caught pre-commit; would have been High in production and silent
**Component**: Enumerate lane / checkpoint / mutation harness
**Status**: Resolved

## What Happened

Phase 5 built the enumerate lane: claim one job, page the profile 20 items at a time, cache each item's `play_addr` on the way past (F1), checkpoint after every page, sleep 3–5 seconds between pages, and stop at `blocked` if the cookie is being refused. One instance, globally, because Douyin rate-limits by IP and not by job (F2).

The interesting part is not the lane. It is that writing the lane proved a guard shipped in phase 4 was wrong — and wrong in the way this project keeps rediscovering, where nothing crashes and nothing alerts and the wrong thing just quietly happens forever.

## The One That Mattered

Phase 4's `checkpoint()` protected against a reaped straggler rewinding its replacement's progress:

```ts
maxCursor: sql`greatest(${jobs.maxCursor}, ${input.maxCursor})`
```

Monotonic in the cursor's numeric value. Reasonable-looking, well-commented, and it had a test that passed.

Douyin's `aweme/post` returns `max_cursor` as the `create_time` (ms) of the **oldest** item in the page, and a profile pages **newest first**. So the cursor **decreases** on every page.

Under `greatest`, the checkpoint takes page one's value and never moves again. And every other signal keeps looking healthy: `enumerated_count` climbs page by page, the lease renews page by page, the job reaches `downloading` with the right `total_videos`, the log line says how many pages it read. Nothing is red. The only symptom is that a job killed at page fourteen resumes at page two — re-spending the exact IP-based request budget the checkpoint exists to protect, on every restart, forever.

### Why no test could have caught it

Because every fake parser in the repo was free to return ascending cursors, and every one of them did. The fixture was easier than reality, and the guard was only ever measured against the fixture.

That is a distinct false-green shape from the four this repo has already logged. The others were assertions that could not fail — wrong key case, an already-scrubbed observation surface, a spy proving invocation rather than outcome. This one is an assertion that *could* fail, against data that *could* have triggered it, fed input that never would. The test was honest; the world it was shown was not.

The fixtures in `enumerate.lane.spec.ts` now page **downwards**, as the real thing does, and there is a mutation that puts `greatest()` back — measured both at the repository and **through the lane**, because a repository test handing over descending numbers by hand is weaker evidence than a lane paging a profile the way Douyin serves it.

### The fix changed the yardstick, not the invariant

The straggler protection is still wanted; "monotonic in the cursor" was just the wrong way to express it. The cursor is now written only when the incoming `enumerated_count` is **strictly greater** than the stored one:

```sql
max_cursor = CASE WHEN :enumeratedCount > enumerated_count
             THEN :maxCursor ELSE max_cursor END
```

`enumerated_count` only ever grows, whichever direction the cursor moves, so "has seen more items" means "is further along" by construction. A straggler has by definition seen fewer, so it still cannot rewind anything. Strictly greater and not `>=`, because two lanes at the same count have no ordering between them and the second to arrive must not overwrite the first.

The phase-4 test that asserted the old behaviour still passes unchanged — its straggler carried a lower count as well as a lower cursor, so it never distinguished the two rules. That is worth noticing: the test was not wrong, it was **under-determined**, and it had been sitting there looking like coverage.

## A Comment That Was Lying, Caught By A Mutation That Stayed Green

`upsertMany` deduplicated repeated items within a page using a `Map`, with a comment explaining that `ON CONFLICT` cannot see a conflict against a row inserted by the same statement and that Postgres raises `cannot affect row a second time`.

The mutation that broke the `Map` **stayed green**. Under the harness rules that is a hard failure, so it got measured instead of argued:

```
INSERT INTO t (id,v) VALUES ('a',1),('a',2) ON CONFLICT (id) DO NOTHING   -> OK, 1 row
INSERT INTO t (id,v) VALUES ('a',1),('a',2) ON CONFLICT (id) DO UPDATE ...-> ERROR:
    ON CONFLICT DO UPDATE command cannot affect row a second time
```

`DO NOTHING` handles it fine. Only `DO UPDATE` raises. The `Map` was dead code and the comment was confidently wrong.

The cost of leaving that in is not the three lines. It is that the next person to consider `DO UPDATE` reads a comment saying it is impossible for a reason that isn't true — so a false comment does not merely fail to help, it actively blocks a correct change. "Mutation stayed green" is the only channel that reports this, which is why it has to stay a hard error rather than a shrug.

## The Cookie-Recovery Test That Would Have Proved Nothing

Plan §5 requires: map `blocked` → `failed`, and the cookie-recovery test must go red.

The obvious test — get stuck, push a fresh cookie, `/retry`, finish — **passes unchanged under that mutation**. `RETRYABLE_STATUSES` is `['blocked', 'failed']`, so both words walk the same path. The test would have read as strong end-to-end evidence and said nothing whatsoever about the distinction it was named after.

Within phase 5's code there are only two places the difference is observable:

1. **The word itself.** Since both are retryable, the status *is* the remediation instruction: `blocked` means rotate the cookie and this job continues; `failed` means the profile is not gettable and a new cookie changes nothing. Getting it wrong does not break the machine — it sends a person to do the wrong thing, which is the exact cost F4 exists to avoid.
2. **Cancellability.** `CANCELLABLE_STATUSES` includes `blocked` and excludes `failed`, because a blocked job is live-but-stuck and a failed one is over. That is a behavioural consequence of the word rather than a restatement of it, and a job wrongly marked `failed` cannot satisfy it.

Both are asserted, with a must-stay-green control that rewords the log line and the reason prose — proving the test is anchored to the status and not to the message. Phase 7's probe adds the third and largest discriminator, when it can finally tell a banned cookie from a private profile.

## Making F2 Measurable At All

"Enumerate concurrency is 1 for the whole system" is not a property of `EnumerateLane`. Nothing inside that class could enforce it. It is a property of how many instances `lanes.module.ts` registers, which is Approach 1's whole argument: concurrency is the number of loops, so there is no semaphore to miscount.

Which means a test that constructs its own lane and watches it behave like one lane is a tautology, and would stay green through precisely the edit that matters. The rate test therefore resolves the **production `LANES` array** out of the container and drives that.

It also needs a positive control, and this is the part worth remembering: `maxInFlight === 1` is only evidence if the instrument can see a 2. A counter wired wrong, or a fake that resolves before its caller yields, reports flawless compliance from a system with six lanes in it. So the same file runs two lanes deliberately and **requires** the probe to report 2.

The pacing is chosen so the red is deterministic rather than probabilistic: each request is held open for 80ms against a ≤30ms inter-page sleep, so a lane is in flight 73% of its cycle and two of them cannot stay out of each other's way.

There is also a must-stay-green control that adds a second **reaper**. If that turned the rate suite red, the suite would be measuring array length rather than request concurrency — different claims, and only one of them is F2.

## The Harness Lied Twice While Being Written

Both were the failure mode plan §5 keeps describing — the harness breaks by reporting confidence — and neither had anything to do with the code under test.

**A backtick in a mutation label.** `run_mutation "trust the lane\`s page-sum ..."` opened a command substitution inside a double-quoted string. Bash executes scripts incrementally, so seventeen mutations ran and then the parse died at EOF, meaning the script never reached its own `restore`. Phase 4's rule-5 pristine trap is what kept the tree clean; without it the next run would have measured a build nobody intended. `bash -n` is now run before trusting a harness edit — and note it does **not** catch the balanced-backtick variant, which parses fine and merely tries to execute `values ()`.

**A fake with a missing method hung the suite silently.** The F2 spec binds a stub to `PARSER_CLIENT`, and `ParserModule.onApplicationShutdown` calls `close()` on whatever is bound there. The stub had no `close()`, so the throw landed inside Nest's teardown, the pg pool and the Redis client were never closed, and jest **hung with no output at all** — not a failure, not a timeout, nothing to read. Rule 6 exists because a mutation can hang instead of failing; this is the same shape one level down, in a fixture.

## Guards Added Beyond The Spec

| Guard | The failure it prevents |
|---|---|
| A cursor that does not advance throws | `has_more` set with the cursor unchanged means the next request is byte-for-byte the last one: an infinite loop spending the rate limit at full speed, every page of which is a healthy HTTP 200 |
| Only 400 is retried or counted toward `blocked` | 404/422 is a contract bug on our side; reporting it as `blocked` sends someone to rotate a cookie that was never the problem — F4's mistake from the other direction |
| An item with no readable `create_time` is dropped | The column is NOT NULL *and* it is the download claim order. Defaulting it to `now()` puts a 2019 video at the front of the queue, and nothing downstream would ever report that as wrong |
| A thrown tick releases the claim, then rethrows | Release so the next tick retries immediately instead of waiting out `LEASE_SECONDS`; rethrow so the base class logs and backs off, so a parser that is down does not become a hot loop |

## Known Gap, Written Down Rather Than Assumed Away

`finishEnumerate`, `markBlocked` and `releaseClaim` are all gated on `status = 'enumerating'`, which stops a reaped lane writing over a row that has moved on. It does **not** cover reaped-and-then-re-claimed: the status is `enumerating` again and these three cannot tell the new claim from the old one.

Closing that properly needs an ownership token. `lane.runner.ts` already assigns one to phase 6 (the lease re-check before `rename`), and it belongs here too when it lands. Today the window is bounded by arithmetic — a drain is seconds, a lease is 300 — and arithmetic is not a guard.

## The Review Found Four Assertions That Could Not Fail

An adversarial pass over the finished code — suite green, mutation run clean — proved each of these by running the mutation, not by suspecting it.

**The jitter test produced a metronome and then asserted a metronome was acceptable.** It injected `random: () => 0`, which pins the draw to the floor on every page, and asserted only that the result sat inside the configured band. Replacing the jitter with a fixed 3000ms left all 30 tests green. The seam is now used to force draws that *differ*, and the assertion compares them to each other.

**The log-leak test asserted the absence of something the lane never holds.** Its only assertion was `not.toMatch(/sessionid|passport_csrf_token/i)`. The cookie lives in the parser fork and is read from Redis — it is never in scope in this file, so no mutation of it could ever violate that. Meanwhile `sec_user_id`, named in the test's own title and genuinely held by the lane, was not asserted at all. Appending the profile URL to the success log line kept the test green. This is the phase-3 shape — an observation surface that cannot carry the thing being looked for — arrived at from the opposite direction: not already scrubbed, just never present.

**A dropped-item count asserted `toMatch(/2/)`.** Any `2` anywhere in the accumulated log satisfied it, and in practice the match came from the job's own UUID — about seven runs in eight. Deleting the entire "N items dropped" clause left it green. Worth dwelling on: this is a *flaky* green, which is strictly worse than a hard one, because it fails rarely enough to be dismissed as noise.

**The behavioural half of `blocked` ≠ `failed` never executed under the mutation it existed for.** The test asserted `toBe('blocked')` and only then called `cancel()`. Under the plan-mandated `blocked → failed` mutation it died on that first assertion and never reached the cancel — so the file contained two assertions about the word and zero independent behavioural evidence. Ordering was the whole problem: `cancel()` now runs first. That is a useful general shape — an assertion placed before the thing it is meant to enable is not coverage, it is a guard on the coverage.

The review also found a title claiming coverage that could not exist: *"does not consume an enumerate attempt"* on a table with no `attempts` column at all (F6's `attempts` is on `videos`). Title drift reads as coverage exactly the way assertion drift does.

## And One Guard With No Test In Either Direction

Every gap assertion used a script containing no 400s, so the pacing on the HTTP-400 retry path was never measured. Deleting `await this.pause(signal)` from that branch left 33/33 green.

That is not merely an uncovered line. Without it the lane fires `MAX_CONSECUTIVE_REJECTIONS` requests back-to-back at whatever rate the parser can refuse them, parks the job, and — because the tick still reports `'worked'` — comes straight back for the next job and does it again. Under a parser-wide failure that is `3 × queue depth` unpaced requests against an IP-based rate limit: the exact ban F2 exists to prevent, arriving down the one route none of the pacing tests covered.

## Two Guards The Review Made Stricter

**The cursor guard only rejected "equal", not "went up".** Equal means the next request is byte-for-byte the last one. Larger is worse: it walks back towards the newest video, page one comes round again, and the lane oscillates forever — lease renewed every page, `enumerated_count` climbing, one request every 3–5s against the IP budget, and `runOnce` never returning, so the job queue behind it never advances either. Rejecting that half is only possible *because the direction is now known* — the same fact the checkpoint fix rests on, used a second time. `cursor === 0` stays exempt, and that is not an edge case: it is how every job starts.

**`checkpoint` discarded a stale write but still answered `true`.** Which tells a lane that has just been overtaken "still yours, keep going" — two concurrent request streams against an IP-rate-limited endpoint, lasting as long as the straggler does. F2 violated by the code written to protect the checkpoint. The predicate was already being computed for the `SET` clause, so it moved into the `WHERE` too, and the lane's existing `if (!stillOurs) return 'lost'` path now stops the straggler dead. This is the one place the honest "arithmetic is not a guard" note became an actual guard.

That change has a consequence worth stating: every checkpoint must now carry strictly more items than the row holds, so the lane must not checkpoint a page that added nothing. It doesn't — an empty page is either the blocked signal or the end of the profile, and both leave the loop first.

Also from the review: `Math.trunc` on the cursor, because `max_cursor` is a bigint and the parser's number coercion accepts non-integers, so `...600000.5` reaches the parameter and Postgres rejects the whole statement — a tick that fails safely and then does it again forever, with nothing in the log mentioning a decimal point. And `release()` in the throw path now has its own `.catch`: without it, a release failure (Postgres gone, the most likely cause) *replaces* the parser error that actually caused the tick to fail, so the log names the second-order symptom and loses the first-order cause.

## What The Review Confirmed

Worth recording, because "we checked and it was fine" is evidence too:

- **The checkpoint rule is sound.** No interleaving loses data. Both `SET` branches are gated on the *same* predicate, so the row always holds a consistent `(count, cursor)` pair from one writer; and the lane only writes a cursor after `upsertMany` has committed that page, so the winning cursor is always backed by inserts down to it. Ties resolve by arrival order, and the loss is bounded to items created between two fetches — re-fetched and deduped, never skipped.
- **No SQL injection**, verified by turning on `log_statement='all'` and reading the emitted statement rather than reading the source: every interpolation is a placeholder, including the one carrying the `::bigint` cast.
- **The F2 positive control is real.** `inFlight` increments synchronously on entry and decrements in a `finally` after an 80ms hold, so a 2 is observable. One fix: `reset()` must also reset `inFlight`, or a request still in flight makes `maxInFlight === 2` satisfiable with no overlap at all — turning the one assertion whose entire job is to prove the instrument can fail into an assertion that cannot.
- **`enumerated_count` has exactly one writer**, which is what makes the new yardstick trustworthy. Now written into the comment, because a phase-7 path that reset it would freeze the cursor again from the other end.

## Numbers

| | |
|---|---|
| Tests | 516 passing, 46 suites |
| New suites | 4 (`enumerate.lane`, `aweme.mapper`, `enumerate-global-concurrency`, `cookie-recovery`) |
| Mutations | 32 must-fail, all red (9 added from review findings) |
| Controls | 2 must-stay-green, both green |
| Real bugs found | 1 shipped in phase 4, 1 false comment, 1 unguarded request-budget path, 2 guards too loose |
| Tests that could not fail | 5 — 1 caught while writing, 4 by the review |

## Lessons

1. **A test is only as honest as the world it is shown.** Four previous false greens were assertions that could not fail. This one was an assertion that could fail, fed input that never would. When a guard depends on the *shape* of external data, the fixture has to model that shape — direction included — not a more convenient version of it.
2. **"Mutation stayed green" is how a false comment gets caught.** Nothing else looks at a comment. A wrong one is worse than a missing one, because it blocks the correct change by pretending it is impossible.
3. **A concurrency limit needs a positive control.** "Max in flight was 1" and "the counter is broken" produce identical output. Prove the instrument can see the violation before believing it did not happen.
4. **Check where the invariant actually lives before writing its test.** F2 lives in an array in a module file, so the test has to read that array. A test of the class would have been well-written, thorough, and worthless.
5. **A neutralised test seam is a new false-green shape.** `random: () => 0` did not *stub* the jitter, it *removed* it — the seam that existed to make the behaviour testable is what made the test vacuous. When injecting determinism, force a value that would expose the bug, not the one the bug produces.
6. **An assertion placed before the thing it enables is not coverage.** Asserting the status and then testing what the status permits means the second half never runs under the mutation the first half catches. Put the consequence first.
7. **A flaky green is worse than a hard one.** `toMatch(/2/)` matched a UUID seven runs in eight. A test that fails 12% of the time when the code is broken gets filed as noise; one that always fails gets fixed.
