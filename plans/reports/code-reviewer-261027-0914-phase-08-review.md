# Phase 08 review: reviewer, rerun routing, publish (03e5b19..HEAD)

Reviewer: code-review skill (`backend-ai-toolkit:code-review`) invoked. Spec compliance, then quality, then adversarial.
No mutation harness was run (entries judged by reading only). `reup` database not touched, compose stack not started, no migration applied.

## Verdict: PASS (no Critical, no High). 5 Medium, 8 Low.

## Verification run

| Check | Result |
|---|---|
| `services/localization`: `uv run pytest -q -m "not live"` | 926 passed, 6 deselected (350 s) |
| `ruff check src tests`, `ruff format --check` | clean (158 files formatted) |
| `services/renderer`: `pnpm test`, `pnpm lint` | 93 passed, eslint clean |
| `services/downloader`: jest `compose-constraints.spec.ts` | 25 passed |
| Throwaway databases | No leak. The publish tests use `scratch_database` (`tests/support/scratch_db.py:46-61`), which drops in `finally`. Only the pre-existing `loc_starter_3564f9157345` remains (left alone; it is from an earlier killed run, not from this run). The fixture also runs `CREATE ROLE localization NOLOGIN` if the role is missing, which is a cluster-level side effect (Low, see L8). |
| Processes | None left by me. Temporal dev server PID 27944 belongs to another session; left. |

No Python type checker is configured in `pyproject.toml`, so there was no typecheck to run for Python.

## Spec compliance (phase-08, plan §0, §5, §6)

- Reviewer in ONE activity (plain Agent, not TemporalAgent), tools are local Python functions, `include_binary_content=False`, only `ReviewVerdict` returned: met. No presigned URL anywhere. Frames are in memory, the temp dir is removed in `finally`.
- Four tools present. `sample_frames` caps (8 per call, 3 calls) are met. `residual_source_text` has no per-review cap (L1).
- Rerun routing is earliest stage, pure function, per-round keys `work/{id}/r{n}/`. Verified in code: script/segments/dubbed.wav/spec go to `r{n}`. TTS clips are keyed by text hash, so a rerun only pays for changed lines (`plan_synthesis` is re-run on the new script key; unchanged lines are cache hits). AC2: layout and Reviewer read `dub.script_key`, so captions equal what was spoken.
- Publish: `output/{id}.mp4` only if `verdict.passed` and no reasons (activity self-check at `publish.py`, independent of the workflow); `review/{id}-g{n}.mp4` for needs_human; `failed` copies nothing. `runs` upsert on `(aweme_id, generation)`.
- Replay: `workflow.patched("review-publish")` is evaluated once, after `dub-audio`. The order of the dubbing commands is unchanged. Activity args are always full-length, so the SDK type conversion works. Old phase-04/05/06 histories still replay; phase-08 histories are recorded from the real workflow with fakes; the suite is green.
- Deviation, acknowledged by the cook: the phase table row "fail `caption`, then pass: only 4b and 7 run again" cannot happen with a pure layout. A `captions` rerun gets the same `spec_sha256`, so decision (c) stops with needs_human (see judgement (c)).

## Findings

### Medium

**M1. `residual_source_text` will produce false "OUTSIDE the cover" findings on a first real run, and one OCR hiccup kills the review.**
`agents/reviewer.py:175-203`, `activities/review_render.py:108-116`, `providers/vision_ocr.py` prompt.
- Failure scenario A: `detect_region` asks "where is the burned-in subtitle band". On the rendered frame it finds the new Vietnamese caption. `_inside` compares it to `cover.region` only (+15% slack). The caption box can be taller than the cover (`caption_box_for` grows the box to `MIN_BOX_HEIGHT`), and when `spec.cover is None` (video with no hard subs) `inside` is always False. Every frame is then reported as "OUTSIDE the cover", although the "text" is our own caption. The Reviewer files a `render` or `captions` issue. `render` issues produce NoRoute, so the run goes to needs_human instead of output/.
- Failure scenario B: `PermanentProviderError("ocr: unexpected response shape")` (a chatty model, no JSON) is raised inside a tool and is not a `ModelRetry`. It propagates out of `agent.run`. It is non-retryable (`_PERMANENT`), so the activity fails and the run is marked `failed` after a good render exists. G5 (bbox quality) was never run, so bbox noise is unmeasured.
- Fix: compare against the caption box (cover union caption rows) and, when `cover is None`, say "the Vietnamese caption is expected here". Catch `PermanentProviderError` in the tool and return "OCR unavailable for these frames" (or `ModelRetry`). Also state in the tool text that it cannot tell Chinese from Vietnamese.

**M2. `glyphs.py` rejects `°` although it is listed as allowed.** `text/glyphs.py:17` vs `:39`.
`_ALLOWED_SYMBOLS` contains `°`, but `°` is category `So`, and the `So/Sk` branch (line 39) fires first, so the allowlist entry is dead. Verified: `foreign_chars("30°C") == ['°']`. Same for `^`, `|` and the Unicode minus `−`. Chinese sources with temperatures are common. Effects: the Localizer is bounced (retries burn tokens, can end in `_gave_up`); worse, a Dubber rewrite that keeps `°` is refused and flags the whole run (M3). No test covers `°` (`tests/text/test_glyphs.py` has no `°`).
- Fix: check `_ALLOWED_SYMBOLS` before the category branches (or drop `°` from the So rule), add `°` and `−` to the test. Optionally verify the font has `°` at startup.

**M3. A glyph-refused Dubber rewrite sends the whole run to needs_human, which means no `output/`.** `activities/dub_script.py:65-68`, `workflows/video_localization.py:539-551`.
Refusal on contacts is an injection signal, so flagging is right. Refusal on a foreign glyph is not: the line keeps its safe old text and nothing is wrong except the line may still overrun. Combined with M2, one temperature in one line removes the video from `output/`, which is the user's top priority. Fix: only contact refusals go to `refused_ids`/flags; glyph refusals are logged and left to the `overflow_unresolved` path that the Reviewer already sees.

**M4. A review that fails for model or transport reasons ends the run as `failed` and leaves no copy of the good render.** `workflows/video_localization.py:498-510`, `activities/review_render.py:188`.
Only `UsageLimitExceeded` is converted. `UnexpectedModelBehavior` (verdict never validates, 3 output retries), `ModelHTTPError` (429/5xx from OpenRouter) and the OCR error (M1) are retried once (`maximum_attempts=2`, 10 s) and then the run is `failed`. The mp4 stays only at `work/.../render-{sha}.mp4`, which has a 30-day lifecycle, and `review/` is not written. A first run on an untested vision model is exactly where this will happen. Fix: on a review that cannot be completed, publish `needs_human` with the render and reason "the review could not be made: ...", or at least raise attempts to 3-4 with a longer backoff for HTTP errors.

**M5. Failures before layout leave no `runs` row, contrary to the progress note.** `workflows/video_localization.py:241-246` (`_localize` and `_dub_with_regeneration` are outside `_review_and_publish`'s try).
The note says an agent that gives up is now "written on the run". That holds only for reruns (`test_an_agent_that_gives_up_during_a_rerun_...`). A first-pass Localizer give-up (now more likely because of the glyph validator and M2), a Dubber give-up, a permanent TTS error, or an `ArtifactMissing` after regeneration still fail the workflow with no `localization.runs` row. Fix: wrap the whole post-prepare section, or correct the note so the operator knows to read Temporal for those.

### Low

- **L1.** `residual_source_text` has no per-review cap (only `tool_calls_limit=24`), so up to 24 x 8 = 192 OCR calls (120 s timeout each) in a 10-minute activity; a looping model burns the activity timeout and a retry. Cap it like `sample_frames` (e.g. 3 calls). `agents/reviewer.py:175`.
- **L2.** A verdict with `pass=true` is published with no consistency check: `check_verdict` (`agents/reviewer.py:213`) accepts `pass=true` with issues present or with scores near 0, and the workflow publishes if `verdict.passed`. Add a floor on scores and reject `pass=true` with issues.
- **L3.** Partial failure of `publish` (S3 copy done, DB down for more than about 30 s of retries): `output/{id}.mp4` exists, the workflow then writes a `failed` row with `output_key NULL`. The state is self-contradicting but recoverable by a generation bump. `activities/publish.py`, `workflows/video_localization.py:512`.
- **L4.** Runbook order: step 2 migrations need the `localization` role (`CREATE SCHEMA ... AUTHORIZATION localization`, `GRANT ... TO localization`), which `db-init` creates, but `db-init` is only started in step 4. This works on this stack only because `db-init` already ran in phase 00. Put `docker compose up db-init` before step 2. Also `261026-localization-outbox.sql` contains its own `BEGIN/COMMIT`, so `psql -1` prints transaction warnings (harmless).
- **L5.** `model_for("reviewer", settings)` builds a new `OpenAIProvider`/HTTP client per review (`workers/run.py:139`) and never closes it. Slow connection leak; cache one model per worker.
- **L6.** `worker-agents` `/tmp` is 1g and shared by all activities on that container; `LLM_CONCURRENCY=8` reviewers each hold a copy of the render. Fine for one video, tight for a backfill. Note only.
- **L7.** Renderer and layout reject frames over 1920x1088 px (`MAX_FRAME_AREA`). 1080x1920 passes, but a 1440x2560 Douyin source fails the run permanently with `ImplausibleCover`-style refusal after dubbing was paid for. Check `check_renderable` at ingest or before TTS instead.
- **L8.** `tests/support/scratch_db.py` creates the `localization` role in the shared cluster if absent. Harmless but a cluster side effect; prefer skipping when the role is missing.

## Judgement on cook decisions

- **(a) needs_human or refused rewrite: render once, review once, copy to `review/`, never `output/`.** Accept. Safe, no wasted reruns, evidence kept. Caveat: it is only as good as what raises the flag; M2 and M3 make a harmless glyph refusal raise it. Fix those and (a) is right.
- **(b) permanent layout/render failures fail the run with reasons on the row, no retry.** Accept for post-dubbing stages (verified: `_record_failure` writes `failed` with `verdict.reasons`, tests at `test_review_loop.py:395,472`). Coverage gap is M5 (pre-layout) and M4 (review failures should arguably be needs_human).
- **(c) at most two reviews; render-stage or unfixable issues straight to needs_human; identical layout not re-reviewed.** Accept. The loop is correct and the routing is deterministic (sorted sets, no wall clock). The `captions` route is in practice dead: layout is a pure function of unchanged inputs, so the rerun always yields the same `spec_sha256` and stops with needs_human. That is honest and cheap, but the spec's "caption fails then passes" scenario is unreachable in production; say so in the phase file.
- **(d) early rejection of CJK/emoji/arrows/full-width in Localizer output and Dubber rewrites.** Accept the idea, with fixes. Vietnamese NFC and NFD both pass (verified `Việt Nam`, NFD `Đường Việt`), ASCII digits and punctuation pass, `₫ $ % × ‘ ’ “ ” — … « »` pass. Bugs: `°` rejected despite the allowlist (M2), `^ | −` rejected, and glyph refusals should not flag the run (M3). Layout's `UncoveredGlyphs` remains as the final gate.
- **(e) production-wiring test requires a production worker to serve every (activity, queue) the workflow schedules.** Accept. It derives pairs from real histories and also checks no test stand-ins are registered. Verified that `review_render` is on `llm`, `publish` on `io`, `layout_captions` on `media`, and `render` is matched against `services/renderer/src/worker.ts`. Limit: it checks registration only, not the MinIO policy.

## Targeted answers

- **Replay safety:** good. Single patch marker, same command order for the old part, histories recorded from the real workflow, control proves the marker is needed.
- **Payloads and leaks:** no bytes or URLs in Temporal payloads; verdict is small (issues <= 20 x 500 chars); images are excluded from spans (control test exists). Untrusted text is wrapped with `<`, `>`, `&` escaped. Reviewer notes reach the Localizer and Dubber only inside the untrusted block.
- **Vision requirement:** `REVIEWER_MODEL` is required at worker start (`ConfigError: REVIEWER_MODEL is not set`), never falls back to the text model. Compose passes it, jest allowlist includes it. Nothing validates that the model reads images; first call will tell.
- **Publish semantics:** copy not move; keys `output/{id}.mp4` (overwritten by a later generation, by design) and `review/{id}-g{n}.mp4`; second publish of the same run is idempotent (ON CONFLICT DO UPDATE on the same key); copy then DB write, both retry-safe. MinIO policy (`infra/minio/init.sh`): `localization` has Get on `work/*`, Put-only on `output/*`, read-write on `review/*`; server-side CopyObject needs exactly Get on source and Put on destination, so it fits. This is checked by reading only; the test store is a file fake and does not enforce policy. A flagged or failed run cannot reach `output/`: the activity refuses unless the verdict passed with no reasons, and the workflow puts every flag into reasons.
- **videos row, outbox, runs:** `runs` has no FK to `videos` and nothing updates `videos`; link is by `(aweme_id, generation)` and the workflow id `video-{id}-g{n}`. Consistent with the outbox design.
- **Timeouts:** TTS cold start (65 s) is inside the provider timeout of 120 s and the 30-minute synthesize activity with a 20 s heartbeat. Render deadline is 5 min + 6 x video length (about 95 min for 15 min of video) under a 2-hour activity timeout with a 60 s heartbeat; consistent. Review 10 min with heartbeats from a separate task (so a long OCR does not trip the heartbeat).
- **Compose:** `REVIEWER_MODEL` only on `worker-agents`; no new ports; renderer env uses only the render S3 key and no model keys or database URL.
- **Runbook command block:** service names and file names all match the compose file and migrations directory; `pg_dump` precedes migrations; migration set is right (downloader outbox, localization core, starter grants); expected log lines match the code (`worker agents serving queues [...]`, `render worker ready (concurrency N)`). Only the db-init ordering (L4) is wrong.

Status: DONE
Summary: PASS, no Critical or High. Five Medium findings; the ones most likely to hit a first real run are the `°` glyph bug (M2) turning into a whole-run needs_human (M3), and the post-render OCR check giving false positives or failing the run (M1, M4).
Concerns/Blockers: The `backend-ai-toolkit:code-review` skill was invoked and loaded. No mutation harness was run (mutation entries judged by reading only); the `reup` database was not touched and no migration was applied. The compose stack was not started. The real Reviewer model, OCR on rendered frames and MinIO policy for CopyObject remain unverified (no real run available).
