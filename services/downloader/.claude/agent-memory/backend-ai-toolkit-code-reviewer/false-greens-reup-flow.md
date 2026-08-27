---
name: false-greens-reup-flow
description: Five recurring false-green shapes found in services/downloader tests — check all five on every review of this repo
metadata:
  type: project
---

`services/downloader` has now shipped "a test that stays green if the behaviour it names
breaks" in phases 1, 2, 3 and 5. Five concrete shapes, all found by mutation, none by reading:

1. **Assertion names the API field, code produces the ORM field.**
   `expect(res.body).not.toHaveProperty('lease_expires_at')` can never fail, because
   spreading the Drizzle row adds `leaseExpiresAt`. Only the mapper unit spec (which
   asserts the camelCase key) caught it.

2. **The observation surface is already scrubbed by the thing under test.**
   A child-process leak test asserted "the cookie is not in the response body" while
   reading the body out of the child's *redacted* stdout. The assertion is a tautology
   restating what the redaction test above it already proved.

3. **The test seam is stubbed in a way that neutralises the property under test.**
   Phase 5: "jitters the delay rather than issuing a metronome" injects `random: () => 0`,
   so the drawn delay is the floor on every page, and then asserts only
   `min <= ms <= max`. Replacing the jitter with a constant leaves it green. When a test
   names a *distribution* property, the assertion has to compare two draws.

4. **The regex is loose enough that unrelated output satisfies it.**
   Phase 5: `expect(logged.join(' ')).toMatch(/2/)` stood in for "2 items dropped". Any
   `2` anywhere passes — the job UUID supplies one ~87% of the time. Match the sentence,
   not the number.

5. **The title claims more than the assertion checks.**
   Phase 5: "never writes a sec_user_id-bearing URL *or a cookie*" only greps
   `/sessionid|passport_csrf_token/` — and the lane has no cookie in scope, so that half
   can never fail; logging the full profile URL keeps it green. Also
   "does not consume an enumerate attempt" — `jobs` has no `attempts` column at all.

**Why:** each one reads as strong end-to-end evidence in review, and the mutation suite
hid #1/#2 by pairing the weak spec with a strong one in a single test pattern
(`test/cookie/ test/redaction/cookie-leak`) — the run went red, but for the other reason.

**How to apply:** when a mutation's test pattern names more than one path, re-run each
path alone before believing the mutation is evidence. Grep e2e response assertions for
snake_case keys the ORM never emits. And read every test *title* as a claim list, then
check each claim has its own assertion — shapes 3–5 are all title/assertion drift.

Related: the repo's own mutation-harness rules in `test/mutations/phase-0*.sh` headers
(empty-diff = not run; multi-mechanism invariants need a must-stay-green control).
