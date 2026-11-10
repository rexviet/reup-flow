---
title: HyperFrames compatibility gate remains open
date: 2026-11-09
summary: "Stable IDs, SRT, audio stretching and isolated CAS pass; Studio boundary still fails."
---

# HyperFrames compatibility gate remains open

Implemented only the requested Phase 1 compatibility spike with exact HyperFrames 0.8.77,
a nested frozen pnpm lock, stable cue style capture, literal SRT, loopback fixture bridge
and host launcher. Real ffmpeg and isolated review-only MinIO checks passed. Native Studio
saved a 42px style through an earlier cue word-count change and canonical regeneration.

The first snapshots were invalid because CSP blocked injected runtime scripts. Verified
exact trusted-script hashes fixed rendering without allowing arbitrary inline scripts.
Node's macOS path expectation and the layout harness's omitted font mount were corrected;
final checks passed: 3 Node, 23 adapter and 52 image layout tests, plus Ruff and CLI check.

The user supplied a visual reference; approximate italic yellow/black styling and source-timed
full-width blur were compared. Both Studio 0.8.77 and a separate exact 0.9.26 probe accepted
bad Host/Origin on a read endpoint. This does not demonstrate a mutation exploit, but the
strict boundary gate remains unmet. A final native-browser reload timed out. Phase 1 remains
unaccepted and Phase 2 was not started. Production contracts and existing checkout work were
preserved. Temporary servers and the isolated MinIO container were stopped. See
plans/reports/cook-261109-2018-hyperframes-phase-01.md for evidence and remaining gates.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
