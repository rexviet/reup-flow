---
title: Scope Workspace to standard VoiceStudio pipeline
date: 2026-11-01
summary: Removed Vbee desks and rebuilt the mock activity path for the normal pipeline.
---

# Scope Workspace to standard VoiceStudio pipeline

## Changes

Workspace now lists only standard-pipeline desks, with Localizer Agent, Dubber Agent and Reviewer Agent. Removed Slot Shortener and Vbee-only activity aliases. Open Temporal queries allow VideoLocalizationWorkflow and WorkspaceSimulationWorkflow, excluding VbeeDubbingWorkflow.

The mock workflow follows the configured single Localizer / code Dubber / JEV Reviewer path, with initial synthesis, correction and resynthesis, audio assembly, captions and review_before_render. JEV is inside review, not a standalone simulated activity. Moved demos to workspace-simulation-voicestudio to avoid replaying changed command order on old histories; old demo executions remain paused. Production processing code and database rows were not changed.

## Verification

87 web tests, 88 downloader Workspace tests and 4 mock activity tests passed. Ruff, web/backend scoped lint, TypeScript checks, Docker builds and diff whitespace checks passed. Served web bundle has the three agent labels and no Slot Shortener/Review JEV label. Live history in the new namespace contains only standard-pipeline mock steps. Real starter and workers remain stopped.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
