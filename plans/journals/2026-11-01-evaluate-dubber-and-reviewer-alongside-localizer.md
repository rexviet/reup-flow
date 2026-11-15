---
title: Evaluate Dubber and Reviewer alongside Localizer
date: 2026-11-01
summary: "Added production-mode evals, versioned controls, real inputs, Langfuse baselines and blinded human packets."
---

# Evaluate Dubber and Reviewer alongside Localizer

## What happened

Followed the Localizer eval sequence for Dubber and Reviewer. User approved the proposed dimensions. Added dataset builders, authored synthetic controls, an experiment runner, offline rescore and human-label export/import. Production agents/workflows were not changed.

Live Langfuse runs completed: Dubber code 15 core + 2 long + 4 real inputs, Reviewer Jev 31 seeded cases + 5 real inputs. Costs including judges total $0.015390971. Reviewer seeded cases have 20 TP / 0 FP / 0 FN and correct verdict/routing on 31/31. Dubber challenge core fit is 0.889, long fidelity/naturalness 0.625; these small samples do not justify prompt changes.

A scorer initially rejected “ba chục” for 30; changed to production missing_numbers and rescored the saved outputs without model calls. Fixed a cleanup-only TypeSafe SDK aclose mismatch after the initial Reviewer run and recovered its report from all 31 verified Langfuse traces. Real Reviewer input extraction required the compose S3 endpoint; no worker/starter was started. Raw real inputs and reports stay local under ignored eval/results.

## Decision

Freeze core synthetic v1 datasets, keep the long supplement separate, record source/input/config fingerprints, and distinguish simulated from measured speech. Existing published runs are references rather than human gold. Keep coverage/challenge/real separate. The durable runbook and evidence summary live in services/localization/eval/README.md; phase 09 points there without closing older unresolved criteria.

## Verification and remaining work

168 relevant eval and agent tests passed; ruff and diff whitespace checks passed. Exported 12 blinded human items per agent. Human labels and judge calibration remain pending. VoiceStudio was unreachable, so all live Dubber candidates used simulated durations; real WAV split/join/measurement path was checked with generated WAVs, but no live TTS fit or listening conclusion is claimed.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
