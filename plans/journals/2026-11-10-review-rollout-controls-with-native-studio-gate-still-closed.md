---
title: Review rollout controls with native Studio gate still closed
date: 2026-11-10
summary: "Implemented migration recovery, authenticated discovery and single-generation canary controls; kept production legacy while live acceptance remains blocked."
---

# Review rollout controls with native Studio gate still closed

## What happened

The migration runner used to infer all migrations from the presence of videos. It now checks known schema evidence, verifies a private backup/clone restore and commits DDL with its ledger row. A database-session termination test proves that the nullable project pointer rolls back if ledger insertion is interrupted.

Review discovery is a DB-only, API-key-protected paginated query. The web Review tab gives local launch instructions, and Workspace/smoke agree on manual-delivery statuses. The canary CLI starts one new recorded review generation without mutating old outbox rows. Real isolated MinIO verified the exact bridge policy and denied other prefixes/Delete.

The full Python regression run exposed a pathlib import in a contract reachable by workflows. Deterministic string validation repaired the import guard and rejects noncanonical aliases to reserved Studio projection files. Broader downloader checks exposed timing-sensitive existing concurrency/shutdown tests and an incorrect assumption that port 5433 was unused; the latter now discovers an unused port.

## Decision

The native launcher still fails closed on the pinned Studio Host/Origin boundary before it can mount review data. Production was observed read-only: legacy delivery remains configured and the project_key column is absent. No migration, provisioning, provider run, canary, activation, service restart or commit was performed against production.

## Next steps

Use docs/hyperframes-review.md for the gated order. Complete native Studio security and persistence, dedicated real-provider round-trip and deployed-identity checks before canary/default activation. Preserve compatible workers for histories that have already recorded project mode. Final check counts and limitations belong to plans/reports/cook-261110-0658-hyperframes-phase-05.md.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.

Final validation also reconciled two exact worker-registration assertions with the
new revoice workflow and redirected all schema-export fixture outputs into its
temporary root. All four initial Linux failures passed the 126-test affected recheck.
The unchanged downloader shutdown test failed in the full run and passed its complete
25-test file recheck; the report retains that full-suite limitation. Test resources
were removed.
