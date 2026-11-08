---
title: Show Workspace progress per video
date: 2026-11-01
summary: "Removed gate UI and added activity-level progress, focused desks and observed handoffs."
---

# Show Workspace progress per video

## Changes

Added read-only run-detail steps with unique scheduled-event IDs, attempts, timestamps and cancellation handling. Repeated visits to the same desk remain separate. Added active video selection, last completion/current work/possible-next summary, real activity history, persistent latest observed handoff and collapsible cost details. Removed both gate renderings. Selected desk state is reconstructed from that video's steps; pending work wins over later completed batches on the same desk. Unknown/loading or failed detail polls do not borrow another video's global state. Restored valid SVG width/height attributes on room rectangles.

## Verification and review

90 web tests and 89 downloader Workspace tests passed, including repeated activities, concurrent activity IDs, closure, selected desk loading and terminal outcome behavior. Lint, TypeScript and Docker builds pass. Independent scoped review findings were fixed. Responsive rendering at 375/768/1440px reported zero errors/warnings. Browser interaction smoke passed selection, hover, detail disclosure, blocked detail API/unknown desks, recovery and close. Live detail endpoint returned ordered unique step IDs and a real Temporal retry attempt.

## Runtime

Standard pipeline mock workflows remain running in workspace-simulation-voicestudio. Real starter/workers remain stopped. No database writes, commit or push. Next step is user visual feedback on the running UI.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
