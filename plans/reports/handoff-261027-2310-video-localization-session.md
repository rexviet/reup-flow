# Handoff: video localization workflow, session of 2026-10-27

Branch `feat/localization-phase-00-infra` (not pushed). Plan: `plans/261026-0147-video-localization-workflow/`.
Written for a fresh session that continues the work. Read this first, then `plan.md` and the phase files.

## 1. Where the work stands

| Phase | State |
|---|---|
| 00 to 08 | Built, reviewed, fixed. A real end to end run works on two real clips. |
| 09 observability and eval | Not started as a phase. Early work done: real LLM cost recording, provider recorded per call, an OCR eval section written into the phase file. |
| 10 acceptance (AC1 to AC8), backfill, docs and runbook | Not started. |

Real runs use VoiceStudio on Colab (STT `whisperx`, TTS `omnivoice`), OpenRouter, Temporal, MinIO, Langfuse, a Node render worker.

Two clips are used for every real run:
- `7575765838708520689` ("757"): 11 s, 4 short lines, ends with a line that starts 403 ms before the end of the video. It ends `needs_human` on purpose: a rerun cannot fix a line with no time left. That is the expected result, not a bug.
- `7665948504426075877` ("766"): 23 s, longer lines. It ends `published`.

## 2. What happened this session, in order of importance

1. **One request per step.** The Localizer and Dubber used to be tool loops (10 and 5 model requests on a 4 line clip). They now answer in one request plus at most one repair request. The old loop stays behind a flag. Commits `f122afb`, `2808470`.
2. **Reviewer without an LLM loop.** The vision Reviewer (14 calls, $0.0175 on one run, 24 images resent each turn, long reasoning resent each turn) is replaced by checks in code plus one call to Jev (TypeSafe's typed decision model) through OpenRouter. The old Reviewer stays behind a flag. Commits `124f38c`, `ae3c2e9`.
3. **Two defects found by the first real run of the new Reviewer**, both fixed: the agents worker had no caption font (`2b0be2b`), and a model request that got no answer waited the whole 5 minute activity timeout (`e127ac5`, now 180 s, no hidden client retries).
4. **Reasoning was the real cost driver.** The same Localizer prompt cost 47 tokens from one OpenRouter provider and about 2,600 from another, and real runs reached 8,813 and 13,646 tokens ($0.023 and $0.036). It depends on which provider OpenRouter picks, not on the video. Reasoning is now disabled for the one request agents with a cap from the line count, and the provider is recorded per call (`4e7d914`, `5b15fd9`).
5. A wrong diagnosis was made and corrected during the session: the slot of a segment was NOT 40 ms in real runs. `text/slots.py` already extends it (minimum 1000 ms, borrows up to 1500 ms of the following silence). Do not trust the earlier theory about 40 ms slots.

## 3. Measured results (real runs, same two clips)

| Run | Mode | Result | Cost | Wall time |
|---|---|---|---|---|
| 757 g10 | tool loops, LLM reviewer, 2 review rounds | needs_human | $0.0488 | about 9 min |
| 757 g11 | tool loops, LLM reviewer, 1 round | needs_human | $0.0233 | 3 min 55 s |
| 757 g12 | one request agents, LLM reviewer | needs_human | $0.0243 | 4 min 33 s |
| 757 g14 | one request, Jev reviewer, reasoning on | needs_human | $0.0269 | 4 min 1 s |
| 757 g15 | one request, Jev reviewer, reasoning off | needs_human | **$0.0047** | **1 min 38 s** |
| 766 g2 | one request, LLM reviewer | published | $0.0104 | 4 min 45 s |
| 766 g4 | one request, Jev reviewer, reasoning on | published | $0.0412 | 6 min 55 s |
| 766 g5 | one request, Jev reviewer, reasoning off | published | **$0.0076** | **2 min 37 s** |

Review costs about $0.0007 (Jev call about $0.00007 plus 4 OCR frames) instead of $0.004 to $0.0175. Render takes 23 to 25 s alone and 41 to 53 s when two videos render at once (CPU contention in the Docker VM; the per phase `render.timing` log line shows `frames` is the cost). Output videos for review are in `data/review/` (`<aweme>-g<N>-localized.mp4`; for 757 the file is the review render because it did not publish).

## 4. Configuration and switches (all in `.env`, `.env.example`, and the agents worker block of `docker-compose.yml`)

| Setting | Default | Meaning |
|---|---|---|
| `LOCALIZER_MODE`, `DUBBER_MODE` | `single` | `tools` restores the old tool loops |
| `LOCALIZER_REASONING`, `DUBBER_REASONING` | `off` | `default` sends the plain request (no reasoning switch, no cap) |
| `REVIEWER_MODE` | `jev` | `llm` restores the vision Reviewer (then `REVIEWER_MODEL` is required again) |
| `REVIEWER_JEV_MODEL` | `typesafe/jev-1.13` | `jev-latest` also works |
| `RENDER_TABS` | 2 | The user chose to keep 2 tabs |

Jev goes through OpenRouter with the existing `OPENROUTER_API_KEY`: base URL `https://openrouter.ai/api` (the SDK adds `/v1/systemone`; `.../api/v1` gives 404). Only the Chinese and Vietnamese line text leaves the worker for Jev.

## 5. How to run a real video (do not run anything automatically; the user starts Colab)

1. Ask the user to start the Colab VoiceStudio runtime. Preflight: `/health` returns 200, `/v1/models` lists `whisperx` and `omnivoice`, `VOICE_ID` is in `/v1/audio/voices`. (`/v1/models` lists only the active STT engine.)
2. Rebuild from the latest commit: `docker compose build worker-agents worker-media worker-render localization-starter`, then `docker compose up -d` the same four.
3. Rerun a video by bumping `generation` in `localization_outbox` (reup database): set `generation=generation+1, state='pending', next_attempt_at=now(), dispatched_at=null, workflow_id=null, dispatch_attempts=0, last_error=null`. Current values: 757 is at 15, 766 is at 5. Workflow id is `video-<aweme_id>-g<generation>`.
4. Status through the Temporal UI API (`http://localhost:8233/api/v1/namespaces/default/workflows/<id>`; history at `.../history?execution.runId=<run>`). Results and cost: tables `localization.runs` (status, verdict, `llm_cost_usd`) and `localization.llm_calls` (stage, model, `provider`, tokens, `cost_usd`) in database `reup`. Langfuse is at `http://127.0.0.1:3100`.
5. A hung workflow can be terminated with the Temporal Python client from inside the agents worker container.
6. The postgres superuser is not named `postgres`; read it from `POSTGRES_USER` in the postgres container.

## 6. Rules the user set (keep them)

- No mutation tests during development. Run all of them once after phase 10 and report once. Never edit `tests/mutations/run-mutations.sh` during a run. Expect stale or NO-TESTS entries (the one request agents and the Jev Reviewer have no harness entries yet).
- A real end to end run comes before phases 09 and 10. Tests without real output are not progress.
- Do not run a video on your own: Colab is switched on by the user. Ask first.
- Voice id and similar values are plain config, not blockers. Contact flagging (M1) stays per spec. `RENDER_TABS` stays at 2.
- Do not say a step was verified unless it was. Hand backs from sub agents are reports, not user approval.
- Replies in Vietnamese, outcome first, plain sentences. The user dislikes slow drip fed instructions and promised monitoring that is not done.

## 7. Technical guardrails that bit this session

- Temporal replay: any change to the order or set of activities in `VideoLocalizationWorkflow` needs `workflow.patched(...)` and re-recorded histories under `services/localization/tests`. The single request change added two markers (`localizer-single-request`, `dubber-single-request`) and a context activity. The Jev Reviewer kept the activity name `review_render`, so it needed none.
- Sub agent types lack the Skill tool for some skills; use `general-purpose` for reviews.
- The Reviewer's frames never go into a Temporal payload, span or link (the video is unpublished). The one request agents put only text in prompts.
- Migrations are raw SQL applied by hand with `psql -v ON_ERROR_STOP=1`, after a `pg_dump`. Applied to `reup` so far: `261027-llm-cost.sql` and `261028-llm-call-provider.sql` (adds nullable `provider`). A recorder that cannot write only logs a warning and drops the row, so apply a migration before the worker that needs it starts. Backup of the schema before the last one: `backups/localization-schema-261027-2305.sql` (gitignored).
- Never print keys. `.env` is gitignored.

## 8. Known issues and open questions (in rough order of value)

1. **Possible routing bug.** In `workflows/review_routing.py`, `_why_no_room` treats `room.slot_ms < 1500` as "a rerun cannot make it fit", but `text/slots.py` gives every segment a slot of at least 1000 ms and most are 1000 to 1541. On 757 g15 segment 0 (slot 1425 ms) had a fidelity finding from Jev (81%) and the rerun was blocked with "has a slot of 1425 ms". The two constants contradict each other. Decide whether the slot rule should be dropped or lowered, and test it. Not changed yet.
2. **Jev thresholds rest on a small set.** 0.8 on any of three questions; nine faithful real lines, largest value 0.73 (the meaning question), smallest defect 0.85. Batching more than about 12 lines in one call dilutes answers (the code caps a call at 12 lines). Run more real clips and compare the two reviewer modes before trusting `jev` as the default. The Jev Reviewer also no longer notices visual oddities (artifacts, black frames). Stretch limits (1.12 and 0.88) and the "residual source text near the caption" rule are untested on real renders.
3. **Localizer quality with reasoning off** was checked on one clip only (4 runs); the phrase 与点点栏目 is still often dropped. Eval is the way to settle this (phase 09). `PROMPT_VERSION` is still `localizer-1` in both modes, so eval cannot tell modes apart by that field. The single mode no longer saves new glossary terms (`glossary_upsert` is not offered).
4. **Timeouts and caps are guesses for long scripts.** Request timeout 180 s; cap `min(32768, 1500 + 300 x lines)` tokens; no real measurement above 4 segments. A cut off answer (`finish_reason` length) is a retryable error; after 6 attempts the run fails with the model error text.
5. **Older small bugs, not fixed:** `review_render` in LLM mode sometimes hit a model timeout; a "Failure exceeds size limit" message; a temp dir cleanup race (`FileNotFoundError` on a frame); the pydantic-ai httpx deprecation warning; a leaked test database `loc_starter_3564f9157345` and two `test_enumerate_*` schemas (not created by this work).
6. **Cost cap per run** was raised once and not decided.
7. **Housekeeping:** `.gitignore` shows as modified in the working tree (not by the last agent; check the diff before committing it); untracked review reports under `plans/reports/`; `~/Downloads/VoiceStudio_Colab_Cloudflare_reup.ipynb` is an outdated copy the user told us to ignore; a temporary socat proxy `reup-s3-test-proxy` on 127.0.0.1:19000 may still exist (check `docker ps`); the OCR error table in `phase-09-observability-and-eval.md` still shows rows "in progress" that are now done (consensus box, cost recording).

## 9. Next steps

1. Review the latest commits (`f122afb` to `5b15fd9`) with `/backend-ai-toolkit:code-review` in a fresh reviewer agent; they were written and checked by the same agent that built them.
2. Look at the two videos in `data/review/` (`...-g15-localized.mp4` and `...-g5-localized.mp4`) and decide whether quality is acceptable with reasoning off.
3. Fix or decide item 1 in section 8 (the routing constants).
4. Phase 09: eval set and metrics, including the OCR eval (spec in the phase file), a fidelity eval to calibrate the Jev thresholds, and a comparison of `single` against `tools` and `jev` against `llm`.
5. Phase 10: acceptance AC1 to AC8, backfill, docs and runbook. Then the one time mutation run for all ten phases and a single report.

## 10. Commits of this session (newest first)

`5b15fd9` reasoning off and cap for the one request agents; `4e7d914` provider per call (+ migration); `e127ac5` request timeout 180 s; `2b0be2b` caption font in the agents worker; `ae3c2e9` Jev review; `124f38c` Jev probe scripts; `2808470` replay histories; `f122afb` one request Localizer and Dubber; `0c572f1` mutation re-anchor; `882e6b5` render phase timing; `6ebebff` render measurements; `4ae7f3b` voice rate during ingest; `4e66d49` stop when a rerun cannot fix; `f6983dd` real LLM cost; and earlier OCR, storage and demucs fixes (`3e14cfc`, `2bf7f26`, `32c15c3`, `7f92132`, `328f924`, `b051473`, `d73170e`, `90e39d1`, `f4aaaf0`).

Full session transcript, if details are needed: `/Users/vietphung/.claude/projects/-Users-vietphung-Data-reup-flow/9aa1a711-c362-45ab-8e48-1a9b19f8df48.jsonl` (large; search it, do not read it whole).
