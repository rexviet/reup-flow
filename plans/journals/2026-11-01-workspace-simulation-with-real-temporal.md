---
title: Workspace simulation with real Temporal
date: 2026-11-01
summary: Added isolated mock activity demos and verified Workspace states against live Temporal.
---

# Workspace simulation with real Temporal

## What happened

Implemented an opt-in Compose overlay and a credential-free simulator in localization.simulation. Stopped the real starter and workers started in this session. Downloader now observes workspace-simulation; real workflows stay in default. Three synthetic workflows repeat success, two TTS retries, and terminal review failure with production activity names.

Initial namespace registration needed time to propagate to history hosts; added bounded startup retries. Restarted the simulator and confirmed existing workflow IDs resume without duplicates.

## Verification

Four ActivityEnvironment tests passed; Ruff lint and formatting passed. Live snapshot polling observed working, retrying, failed, activity_completed, activity_failed, activity_retrying and handoff. Detail endpoint returned HTTP 200 with phases. Temporal and Postgres sources reported ok.

## Limits and operation

This demonstrates Workspace behavior, not production workflow payload contracts or routing. No database writes, provider calls, media processing or actual publishing. Existing Postgres runs remain visible; mock costs are zero and no mock gate records are seeded. The overlay and restoration commands are documented in docs/agent-workspace.md. Simulation remains running for the user; real workers/starter remain stopped. No commit or push.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
