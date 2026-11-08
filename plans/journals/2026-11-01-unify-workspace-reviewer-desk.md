---
title: Unify Workspace reviewer desk
date: 2026-11-01
summary: Agent labels now identify actual agents; JEV activity and usage map to Reviewer Agent.
---

# Unify Workspace reviewer desk

## Changes

Renamed Localizer, Dubber, Slot Shortener and Reviewer desks with the Agent suffix. Removed the separate Review JEV desk and mapped review_jev plus generated agent__review_jev names to reviewer. Kept Media, Voice, classifier and detector labels unchanged, reflecting their implementation as processing steps or direct model calls. Updated Workspace documentation.

## Verification

Web: 87 tests pass. Downloader Workspace: 84 tests pass, including the catalog/layout drift check. Web lint, scoped backend lint, both TypeScript checks and git diff --check pass. Rebuilt web and nest images and applied them with the simulation namespace overlay; real workers remain stopped.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
