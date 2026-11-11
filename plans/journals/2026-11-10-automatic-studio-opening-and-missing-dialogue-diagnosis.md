---
title: Automatic Studio opening and missing dialogue diagnosis
date: 2026-11-10
summary: Enabled local review delivery and a persistent Mac watcher; traced missing dialogue to VAD filtering.
---

# Automatic Studio opening and missing dialogue diagnosis

## What happened

The user requested review_project for extension downloads and automatic Studio opening
for both ready_for_review and needs_human. A Compose override and login LaunchAgent
now enable this on the current Mac. Discovery uses the existing starter DB identity in
a read-only transaction; the host gets only numeric project identities. The watcher
persists its baseline and opened generations, queues while another Studio is open,
and launches through the existing loopback/Host/Origin guarded launcher.

## Verification and limits

Eleven Node tests and twelve focused Python tests passed, plus Ruff. Real DB discovery,
installed LaunchAgent, persistent baseline and deployed delivery mode were verified.
A reinstall race in launchctl was repaired with bounded bootstrap retry and repeat
installation passed. The first automatic opening of a genuinely new completion has
not yet occurred; existing generations are intentionally excluded from first startup.
The user's current Studio remains running. AgentWiki publish skipped.

The reported missing dialogue around 1:56 was absent from ASR input because full-stem
Silero VAD at 0.5 omitted it. A real comparison at 0.25 retained the speech and STT
returned text. Two crop lengths returned different words, so exact wording remains
uncertain. The worker now uses 0.25; existing generation-3 caption/audio artifacts were
not manually corrected or regenerated. No schema migration or old-video reprocessing ran.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
