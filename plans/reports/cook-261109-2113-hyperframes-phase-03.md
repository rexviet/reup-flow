# Phase 03: Durable review projects

Phase 03 implements generation-isolated preparation, immutable review revisions and a
durable HEAD pointer. Review delivery remains disabled by default. Phase 01 Studio
compatibility gates remain open; Phase 04/05 and production rollout are outside this invocation.

## Delivered

- Starter freezes `LOCALIZATION_DELIVERY_MODE` in each VoiceStudio input; Vbee and
  other providers retain legacy delivery. A new Temporal marker selects project delivery.
  Historical inputs keep legacy work keys, decoder behavior and render hashes.
- A conditional source GET pins ETag/version and checksum before concurrent preparation.
  Ingest/OCR use that snapshot; transcript, subtitle, script, dub and mixed audio producers
  use `work/{aweme}/g{generation}/...`. Consumers reject foreign generation or script version.
- Bundle dependencies are copied into content-addressed assets under the same review
  generation. Manifest creation is conditional; HEAD creation uses `If-None-Match: *`;
  subsequent HEAD updates use ETag CAS. Initial retries resolve the existing HEAD.
- The manifest records source provenance, stable cue/source IDs, caption status/reason,
  active spoken text/placement, voice/provider/model/engine, source masks and checksums.
  Materialization resolves HEAD and verifies dependencies into a self-contained folder.
- HTML has muted source video, one active mixed audio track, retained cue clips, editable
  font/style, yellow captions with black outline, and source masks independent of blank cues.
  Caption/reason data is escaped; pinned Studio runtime hashes and the exact player-script
  hash are allowlisted. Full Studio browser compatibility still belongs to the open gates.
- Project delivery ends at `ready_for_review` or `needs_human` without final render/output.
  Frame-only/unavailable/failed reviewers require human review. Publishing verifies HEAD,
  provenance and manifest quality before storing its pointer. Legacy early gates retain behavior.
- Added project/HEAD schemas and nullable `runs.project_key` up/down migration files.
  Production migration is reserved for Phase 05. README and environment example document
  the backend contract and default-disabled rollout boundary.

## Verification

| Check | Result |
|---|---|
| Project contracts/storage/build/workflow/generation tests | 31 passed |
| Cold-open after removing source/work/cache dependencies | 10 passed in project activity group |
| Media/layout/review/dub/synthesis/worker/preparation checks in raqm image | 309 passed |
| Migration and publishing, including real project-key upsert, in scratch Postgres | 35 passed |
| Starter dispatch/rollback and existing integration tests | 31 passed |
| Legacy replay, Vbee and affected workflow/contracts | 534 passed |
| Production worker registration after final wiring cleanup | 44 passed |
| Ruff, schema export/check, unchanged EditSpec schema, diff whitespace | Passed |

Counts overlap across runs and are not a distinct-test total. The local macOS Pillow
lacks raqm; font/media checks ran with current source/tests/schema/font mounted read-only
into `reup-localization-tests:latest`, using `/opt/venv/bin/python`.

Real running MinIO also passed conditional create, stale ETag rejection, conditional
multipart completion at 9 MiB, identical-byte reuse and checksum-mismatch protection.
The isolated probe objects were removed. The primitive choice follows the
[AWS conditional-write contract](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
with compatibility established empirically on this backend.

Tests cover competing writers, 409/412 handling, source replacement between HEAD/GET,
crashes at asset/manifest/HEAD/DB stages, edited HEAD preservation, cold-open, blank and
escaped captions, and actual interleaved ingest/STT/OCR from two captured source versions.

Postgres was backed up to ignored `backups/pre-phase03-validation-*.sql` before scratch
migration validation. Existing production tables were not migrated. No service was restarted.

## Review and remaining boundary

Acceptance/security/regression review was performed inline with ak-code-review. The
ak-cook simplifier made scoped behavior-preserving changes to project/storage code.
Initial test failures came from missing host raqm, incorrect harness paths/URLs and a
workflow test class importing I/O into Temporal's sandbox; the harness was corrected
without weakening assertions or application checks.

The workspace already contained substantial uncommitted work. This invocation preserved
that work, created no commit, and only executed the requested phase. Phase 01 remains
unaccepted. Companion editing/revoice and operational activation remain Phase 04/05 work.

Unresolved questions: None.
