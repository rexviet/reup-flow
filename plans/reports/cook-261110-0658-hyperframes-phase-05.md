# Phase 05 validation and rollout

The migration, review discovery/API/UI, status consumers and canary controls are implemented.
Acceptance remains open: the native Studio Host/Origin gate rejects the pinned server,
so no production schema migration, provider fixture, canary, default flip or deployment ran.
Read-only live checks confirm the starter remains `legacy_render` and production lacks
`localization.runs.project_key`.

## Delivered

- The migration runner verifies a private backup and clone restore before applying changes.
  It recognizes complete ledger-less schemas from table/column/type/index/constraint evidence,
  rejects partial or unknown surfaces, and commits each migration with its ledger entry under
  a transaction advisory lock. Standalone migration wrappers remain compatible; unsupported
  transaction control is rejected. `project_key` remains nullable, with no legacy backfill.
- `GET /api/reviews` uses global API-key protection, strict bounded pagination, deterministic
  ordering, same-snapshot counts and response validation. It only reads DB locators, with no
  Temporal or object-store access. The existing DB principal is unchanged.
- The Review tab shows both review terminal states, local launcher instructions and unavailable
  prerequisites. Workspace enums, labels, progress and activity catalog agree; deterministic
  project/revoice activities reuse existing desks. Hash navigation opens discovery from run detail.
- A starter-owned `canary` command validates identity, voice, source and capacity, rejects old or
  duplicate generations and starts exactly one recorded `review_project` input without modifying
  the outbox/default. Compose passes mode only to the starter and still defaults to legacy.
- Smoke defaults to review delivery, pins the generation, verifies absent output pointers and
  materializes durable HEAD/dependencies with checksums. `needs_human` with a valid bundle exits 4.
  Explicit legacy mode retains published success; `--run` inspects a canary without enqueueing.
- Bridge Compose isolation now has a liveness probe. The exact checked-in MinIO policy passed
  against an isolated backend and fresh review identity; this is not production provisioning.
- The rollout runbook owns migration, credential provisioning, canary, activation and rollback
  order. Compatible workers must remain available for already-recorded project/revoice histories.

## Verification

| Check | Result |
|---|---|
| Real Postgres migration runner, including session termination before ledger commit | Passed |
| Smoke exit/mode/generation and invalid-bundle orchestration | Passed |
| Starter, recorded-mode workflows, migration and replay | 449 passed |
| Architecture, manifest, interleaving, project and bridge regression after contract repair | 84 passed |
| Durable bundle verifier, valid and corrupt dependencies | 2 passed |
| API/auth/Workspace/architecture/Compose and bootstrap focused set | 140 passed |
| Web, including Review navigation and paging | 96 passed |
| Host launcher unit tests | 5 passed |
| Exact bridge policy on isolated real MinIO | 1 passed |
| Downloader full suite, final run | 871 passed, 1 failed; unchanged shutdown file recheck: 25 passed |
| Builds, ESLint, Ruff, schema freshness, Compose/shell/JS syntax and docs links | Passed |
| Dedicated real-provider/Studio round-trip | 1 skipped; prerequisites absent |
| Native Studio launcher with empty scratch project | Rejected by Host/Origin gate |
| Full Linux Python suite | 3,384 passed, 4 failed, 8 deselected; all four repaired paths passed the 126-test Linux recheck |

Counts overlap. Focused rechecks are reported separately; they do not turn a failed full run
into an all-green full run. The full downloader's shutdown failure was an AbortError; no
lane implementation or assertion was weakened. Its production source and tests are unchanged.
The full Linux failures were the forbidden import, a schema-export fixture that redirected
only EditSpec while the exporter now writes three schemas, and two worker-list assertions
that still expected only the two older workflows. The import was repaired without relaxing
the scanner; the fixture now redirects all outputs into its temporary root; worker tests
assert the exact three-workflow registration, including revoice. The final affected Linux recheck passed all 126 tests, including architecture/contracts,
schema export, queue limits, Vbee/agent wiring and revoice workflows.

Checks used installed Node 24/pnpm 11 and a Linux raqm-capable test image. The stale test
image initially lacked Starlette/Uvicorn; a frozen dependency sync inside the disposable
container resolved collection. No package-manager or host dependency configuration changed.

Raw local logs are ignored under `data/phase05-*`; disposable backup artifacts remain in
ignored `backups/`. No credentials are included in this report.

## Review and limitations

The full regression run exposed a shared contract's forbidden `pathlib` import. It was replaced
with deterministic string validation without weakening the workflow import guard. Canonical
Studio sidecar paths are required, closing aliases to reserved projection/HEAD files; focused
architecture, manifest, generation and bridge tests cover the repaired behavior.

Inline ak-code-review also corrected disposable-DB cleanup ownership, a bootstrap test's
incorrect assumption that port 5433 was unused, and smoke credential transport/error handling.
The unchanged download concurrency test failed in an earlier full run and passed its
focused recheck; the final full-suite shutdown failure and its recheck are recorded above. The ak-cook simplifier
made scoped edits; no independent reviewer approval is claimed.

The pinned native launcher was exercised with an empty scratch project under Node 24.
It failed closed at the Host/Origin gate before mounting review data; port 3002 was released.
The dedicated live provider/Studio round-trip was skipped because prerequisites are absent.
Native close/reopen/style/SRT and actual one-cue provider acceptance remain required before
canary or activation. The tested isolated identity does not prove production bridge credentials.

No application service was restarted or deployed, old video reprocessed, production identity
provisioned, or commit created. Remaining work is live acceptance followed by the gated rollout.

Unresolved questions: None. The blockers are technical acceptance gates, not product decisions.

All task-owned containers and their Temporal/MinIO children have exited. The isolated
Postgres was removed; ports 3002, 5432 and 18444 are free. Plan files retain partial
acceptance, and the local plan index was reconciled. A local journal was recorded;
AgentWiki publish skipped.
