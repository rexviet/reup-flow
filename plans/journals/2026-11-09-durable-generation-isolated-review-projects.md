---
title: Durable generation-isolated review projects
date: 2026-11-09
summary: Phase 03 stores immutable review bundles behind durable HEAD and ends review workflows without rendering.
---

# Durable generation-isolated review projects

Phase 03 was executed with ak-cook --auto on the existing dirty workspace. A source snapshot
now precedes parallel preparation, and every new work producer uses generation-scoped keys.
Review dependencies are copied into content-addressed assets. Immutable manifests are
committed before HEAD; retries preserve existing reviewer revisions and database pointers.

The running MinIO passed conditional create, ETag CAS and conditional multipart completion.
Cold-open tests remove source/work/cache objects before materializing the bundle. Replay,
Vbee and affected contracts passed 534 checks; media/font checks passed in the raqm image;
migration and publishing were validated against throwaway databases after a Postgres backup.
Host raqm and several test harness path/import errors were corrected through the appropriate
harness, without weakening application checks.

Review delivery stays disabled by default. Phase 01 Studio compatibility gates remain open;
companion/revoice and production activation remain Phase 04/05. No production migration,
service restart, commit or publication occurred. The execution evidence is in
plans/reports/cook-261109-2113-hyperframes-phase-03.md.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
