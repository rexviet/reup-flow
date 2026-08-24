# Phase 1: Three Green Tests That Proved Nothing

**Date**: 2026-08-24 13:10  
**Severity**: Caught pre-commit; would have been High in production  
**Component**: Parser fork / secret scanning / test suite  
**Status**: Resolved with Follow-ups

## What Happened

Phase 1 shipped: outer repo commits `b698013`, `4a24ef5`, `c9efcd1`; fork `5fe4e4d`. Twenty passing test cases. Patch held to 37 changed lines in upstream-owned files. The work was done and the tests were green. None of the three defects below reached a commit — a code review and mutation testing caught them first. That is the point of the entry: they were all *green*, and green was not the signal it looked like.

Three separate times, something passed when it shouldn't have. In each case, passing was not evidence that the check worked—it was evidence that the check didn't run at all, or ran blind.

## The Pattern

Three false negatives, each exposed by a different tool: mutation testing, a proof script driving two sequential event loops, and a planted-cookie canary. None are unusual techniques. What they have in common is that each one *breaks the thing on purpose* and demands the check notice. The guards themselves were never asked to fail, so nobody found out they couldn't.

And the same trap one level up: twice a mutation `sed` silently matched nothing after a refactor, so "the suite still passes" was a bad mutation, not a real gap. Verifying the verifier includes verifying that the verification ran.

## Technical Details

### 1. Secret Scan That Couldn't See Cookies

**Status**: PASSED (gitleaks config validation). **Actual**: Config was scanning the wrong patterns.

Gitleaks default ruleset was run against pristine upstream `config.yaml`. Result: 3 findings. Inference: "The scanner works; it caught the upstream author's cookie."

Mutation test: Add a realistic Douyin cookie—`sessionid=<32 hex>`, `sid_guard=<32 hex>`, `passport_csrf_token=<32 hex>`. Scan result: **0 findings**. The default ruleset has no cookie rule at all. The 3 previous findings were all `generic-api-key` fires on high-entropy base64 fields (`fpk1`, `bd_ticket_guard_client_data`, `bit_env`) that *happened* to sit in that cookie. A plain-hex session token scored zero.

**Fix**: Custom rule in `.gitleaks.toml` (douyin-session-cookie) + CI canary that plants a real cookie every build and fails if the scanner stops detecting it. The canary is load-bearing—without it, the next refactor silently breaks the rule.

### 2. Test Suite Blind to Its Own Regression

**Status**: 4 passing tests including `test_cookie_never_in_logs`. **Actual**: The suite couldn't see the regression it was written to catch.

First implementation printed cookie to stdout on the header path. Test was: capture `caplog.records`, assert no cookie value in records. Mutation: Re-add the `print(cookie)` on exactly the line we'd deleted. Test result: **PASS**. The historical log leak was to stdout, not to the logging module. The test was validating the wrong output stream.

Later: logging `str(e)` instead of `type(e).__name__` in exception handling also passed all 4 tests. `getMessage()` was blind to `exc_info`, so a later refactor to use `logger.exception()` would have dumped the full traceback past the guard.

**Fix**: Mutation testing revealed the gap. Actual test now parametrizes both sync and async paths, parametrizes cookie sources (Redis / env fallback), and includes a `test_concurrent_headers_not_serialized_by_slow_redis` that was not in the original 4. Total: 20 test cases.

### 3. Async Cache Bug Hidden by Fixture Hygiene

**Status**: 16 passing tests. **Actual**: The bug only manifests between tests; fixture reset erased the crime scene.

Sync Redis call was blocking the event loop (5 concurrent calls: 5.03 seconds, zero loop ticks). Fixed with `redis.asyncio`. This introduced a module-level async client cached across event loops.

Scenario: Event loop A calls `get_cookie()`, Redis client is created and bound to loop A. Loop A closes. Event loop B starts, calls `get_cookie()`. Client tries to use loop A's socket—`RuntimeError`. Exception is caught and swallowed. `_warned` latches to True. Cookie **stays frozen at empty for the process lifetime**, single warning.

Sixteen tests passed because the autouse fixture reset `_async_client` to None before every test. The fixture that makes tests order-independent had erased the state where the bug lives. The bug surfaced only from `scripts/prove-cookie-swap.py`: two sequential `asyncio.run()` calls against a real socket. That's not a test; it's an escape hatch.

**Fix**: Cache per-loop with rebuild on new loop. Test: `test_get_cookie_async_rebuilds_client_across_event_loops`. This test **does not** reset `_async_client` in teardown—that's the entire point. Also: `_warned` latch replaced with 60s window + counter to avoid silent outages after the first failure.

## What We Tried

1. Validated gitleaks by running it → caught upstream's cookies (turned out to be high-entropy false positives, not the real pattern).
2. Wrote tests against logging output → missed stdout leaks and exception message leaks.
3. Ran test suite 16 times → test hygiene fixtures erased the evidence.

All three attempts were reasonable. All three proved nothing.

## Root Cause Analysis

The pattern: **Trust in the methodology, not in the execution.** 

Secret scan is "a good idea," so running it once and seeing results is "good enough." Tests exist and pass, so the code is "tested." The suite runs cleanly, so tests are "catching bugs." But none of these are *verification*—they're *assumptions about verification*.

The fix for all three was the same shape: **introduce a deliberate mutation that the check is supposed to catch, and assert the check fails**. For scanning: plant a cookie and assert the scanner detects it. For tests: break the invariant and assert tests go red. For async: make two event loops where one is the assumption's coffin, and assert the fix detects it.

This is not a novel technique. It's called *mutation testing* for a reason.

## Lessons Learned

1. **A passing check that you haven't verified is not a passing check.** Run it with a known-bad input and watch it fail. If it doesn't fail, you don't have a check—you have a false sense of control.

2. **Fixture hygiene is a double-edged sword.** Reset-before-every-test is correct and prevents test coupling. It's also the exact condition that lets bugs live between tests and die inside tests. Deliberately write at least one cross-fixture test per complex component that breaks that hygiene.

3. **"The test suite is green" is not evidence that the invariant is upheld.** The test suite being green is evidence that you ran the suite. Whether that suite actually validates what it's supposed to is a separate claim that needs proof.

4. **Plan DoD: "≤50 lines changed"** was literally unachievable (37 lines in upstream files + 121 new + 883 docs/tests = way over 50) until someone reread the source requirement and asked "did you mean changed *in upstream files*?" The gate is good; the wording created a false DoD that we retroactively split into three gates. Reword DoD to specify *what* is being gated.

## Next Steps

- [ ] **CI canary for scanning**: The `.gitleaks.toml` custom rule is now live, but the canary step must remain in CI. If someone refactors the scanner config, it will fail. That's the point.
- [ ] **Per-loop async client**: Verify the `test_get_cookie_async_rebuilds_client_across_event_loops` test actually exercises the code path it claims to. Mutation: Remove the per-loop cache logic and assert this test goes red.
- [ ] **Logging test**: Run `test_cookie_never_in_logs` with a manual `print(cookie)` injection and confirm it fails. Then confirm the fix removes that failure.
- [ ] **DoD rewrite for Phase 2**: "≤50 lines changed in upstream-owned files" replaces the previous gate. Also note: "Test suite passing" means "test suite passes AND mutation testing would fail if the invariant is broken." That's not in the suite yet—it's in the proof script. Move it into CI.

**Status:** DONE | DONE_WITH_CONCERNS  
**Summary:** Phase 1 shipped with three false-negative checks (scanning, logging, async isolation). All three were exposed by introducing deliberate breaks and verifying the checks caught them. The lesson is bitter: passing tests are not evidence of correctness, only evidence that tests were run.

**Concerns/Blockers:** 

- Phase 2 will integrate the parser health gate. If the async cookie cache bug pattern appears elsewhere, it will surface as "parser intermittently healthy after Redis restart" (once a day in production because that's when someone restarts Redis). The fix is unit-tested now, but the integration test (two event loops in a real parser lifecycle) still doesn't exist. That's Phase 2's job.
- Upstream's `POST /update_cookie` endpoint was deleted to force the pull model. But if upstream adds it back in a future version with the same shape, a careless merge conflict resolution could restore both the endpoint and the three `print()` statements at once, defeating the patch. `PATCH_NOTES.md` has the flag, but `PATCH_NOTES.md` is advisory—it's not enforced by CI.
