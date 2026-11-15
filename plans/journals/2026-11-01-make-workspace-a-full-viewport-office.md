---
title: Make Workspace a full viewport office
date: 2026-11-01
summary: Removed auxiliary panels and made the desk canvas fill the browser viewport.
---

# Make Workspace a full viewport office

## Changes

Workspace now uses a fixed viewport canvas with compact Jobs/Workspace navigation, a native video selector and a connection indicator. Removed the mounted progress, history, result/cost and event-log panels. Detail polling remains internal to derive accurate per-video desk states. Portrait screens reflow the same rooms and desks vertically; handoff endpoints follow those positions. Jobs retains its existing centered page layout.

## Verification

90 web tests, 2 catalog/layout drift tests, web lint, TypeScript and web Docker build passed. Browser smoke verified a 1440x900 canvas at a 1440x900 viewport, no auxiliary panels, video selection, portrait viewBox and no document scrolling at 375x812. Rendered screenshots at 375/768/1440 widths had zero errors; three checker warnings refer solely to the intentionally screen-reader-only video label. Screenshots reviewed in /tmp/reup-workspace-fullscreen/. Production workers remain stopped; simulation is still running. No commit or push.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
