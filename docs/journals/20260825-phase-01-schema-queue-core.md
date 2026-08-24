# Phase 1 — the queue core, and four tests that were green before they were earned

**2026-08-25** · `services/downloader` · plan `260824-2152-douyin-downloader`

Phase 1 is three tables, two repositories, and one migration. On paper it is
the most boring phase in the plan. In practice it is the phase where the
"DB is the queue" decision either becomes real or quietly becomes a lie, so
almost all of the work went into making the tests capable of failing.

## What shipped

`profiles` / `jobs` / `videos`, a hand-written forward + rollback migration,
and the claim/reaper repositories. F1, F3, F5 and F6 all landed as columns
now — adding a column to an empty table is free, and adding one to a table
with a hundred thousand rows in it is a migration and a maintenance window.
F7 landed as documentation, as the plan asked, and then as a test, because
documentation does not fail the build.

## The part worth writing down

Four EXPLAIN tests passed on their very first run.

That should be an uncomfortable feeling, and on this repo it now is. A test
that has never been red has never demonstrated anything: it has demonstrated
that it runs. The whole point of those four tests is that a missing partial
index changes *no answer at all* — every correctness test in the phase stays
green while each claim seq-scans the archive. So the only evidence that they
work is watching them fail. Dropping each index in turn produced four
distinct reds, and only then were they worth keeping.

The same reasoning drove the shape of the schema-drift test. Two artifacts
describe the same three tables — the migration an operator runs, and the
Drizzle definition every query compiles against — and nothing in the type
system ties them together. Comparing them one-directionally would have
missed an extra column on either side, so the comparison is a whole-map
equality in both directions. Comparing index *names* would have missed a
swapped column order, which keeps the name, keeps the partial predicate,
and is still chosen by the planner. That one was found by asking "what
mutation would survive this test?" rather than by anything failing.

## Three places the plan's SQL sketch was not quite the plan's intent

The claim/reaper SQL in the phase file is a sketch, and following it
literally would have produced a bug the phase file itself describes as
undesirable in prose two paragraphs later.

The reaper sketch hands an orphaned video back to `pending` unconditionally.
But the claim query filters on `attempts < max_attempts`, so a video that
has burned all three attempts goes back to `pending` and is then invisible
to the claim query *forever* — while every progress count still reports it
as outstanding work. The prose right below the sketch says three crashes
should drop a video to `failed`. The prose was right; the sketch was
shorthand. All three give-back paths — voluntary release, reported failure,
reaped orphan — now route through one `CASE` expression, because they all
face the same question and answering it differently in three places is how
you get a state machine nobody can reason about.

The second was `jobs.checkpoint()`. A lane that has been reaped can still be
in flight when its replacement has already paged past it, and its late
checkpoint would drag `max_cursor` backwards and make the replacement
re-enumerate pages it already had — which costs requests against an
IP-based rate limit, which is the one budget this whole system is built
around. The fix is not an ownership token and a new column; it is
`greatest()`. A cursor that only ever advances is the correct semantics for
paging anyway, and monotonicity makes the handover safe for free.

The third is a race that is deliberately *not* fixed here. `markDone()` does
not check that the caller still holds the lease, so a reaped worker that
finishes anyway can mark a row done that someone else is now downloading.
Adding a lease check here would look like protection while providing none —
a live lease says nothing about *whose* lease it is. That race closes in
phase 6 at the only point that can close it: a `.tmp` name unique per claim,
and a lease re-check immediately before the rename. The comment saying so
is in the code, not just here, because the next person to read `markDone()`
will be looking at the code.

## Carried forward from phase 0

The overlap-and-hold concurrency harness moved from the spike directory into
shared test support and is now used verbatim by the real claim suites. The
lesson it encodes — `Promise.all` over Drizzle transactions is parallel
dispatch, not overlapping execution, and Postgres's `LockRows` will quietly
self-heal a missing `SKIP LOCKED` when every claimer commits fast — applies
to `videos` exactly as it applied to `spike_items`. Re-deriving it would
have meant re-learning it.

The DDL safety guard moved too, and got more important on the way: phase 0
only ever dropped a table it had created itself, while these tests drop the
real ones. Each db suite now runs in its own per-worker Postgres schema,
which keeps the suites parallel, keeps them from racing each other on
identical table names, and keeps the DDL out of `public` entirely.

## Score

74 tests green. Eight mutations, each printing the diff it actually applied,
each caught. `test/mutations/phase-01.sh` treats a mutation that matches
nothing as a hard error rather than a pass — that specific failure has
silently reported "caught" twice on this repo, and it is not going to get a
third.
