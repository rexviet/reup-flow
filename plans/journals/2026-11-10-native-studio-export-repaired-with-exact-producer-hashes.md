---
title: Native Studio export repaired with exact producer hashes
date: 2026-11-10
summary: Exported full 189.67s MP4 after repairing runtime CSP and root-composition materialization.
---

# Native Studio export repaired with exact producer hashes

## What happened

Preview worked while native export timed out waiting for __hf.seek/duration. Removing
our competing standalone player and duplicate root HTML dependencies made the pinned
check pass, but actual export still failed. Debug producer logs proved preview-runtime
hashes were insufficient: producer injection adds one newline and two trusted scripts.

## Repair and proof

Added exact hashes for pinned 0.8.77 runtime/bridge and Studio's 24/30/60fps render
setup, preserving strict script CSP. Compiler data fonts/images are allowed without
arbitrary script execution. Materializers retain hashed HTML bytes as .html.blob;
modified dependency copies fail rather than losing edits. Backed up current native
projection and regenerated g3/new extension g1 through CAS with equal captions/audio.

Forty focused tests, Ruff and pinned check passed. A separate read-only review checked
exact producer bytes and hashes. Actual native Studio export completed; ffprobe
verified H.264/AAC, 1024×576, 30fps, 189.666667 seconds and 69,715,996 bytes. A decoded
frame confirmed caption/box styling. Studio remains open for the user; auto-open
LaunchAgent is restored and the next review waits behind it. No DB change, final
pipeline publication or translation rewrite occurred. AgentWiki publish skipped.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
