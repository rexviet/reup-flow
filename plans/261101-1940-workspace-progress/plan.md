---
title: Workspace progress per video
status: completed
---

Outcome: remove the gate UI and let an operator see the selected video's completed,
current and possible next work, with a real ordered activity history and matching desks.

- [x] Extend read-only run detail with activity-level steps, preserving repeated visits.
- [x] Add active video selection, a progress summary and actual activity timeline.
- [x] Remove the gate section and focus desk state, handoff and event log by video.
- [x] Verify tests, lint, types, live API and responsive rendered views.

Guardrails: retain the standard pipeline only, real Temporal with mock activities for
the demo, no database writes, no restart of production workers, no invented completion
or deterministic claim about a conditional next step.
