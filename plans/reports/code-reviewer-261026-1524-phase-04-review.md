# Phase 04 review: starter, workers, replay, tracing, preparation activities

Range: d939825..HEAD (15d3741, e6b233e), branch feat/localization-phase-00-infra.
Judged against: phase-04 spec, plan.md section 0 (six principles), section 5 (mutations), section 6 (env).
Verified by running: `uv run pytest` on tests/workflows, starter, workers, replay, demucs_safe, dockerfile, worker_settings, tracing, voices: 101 passed (local Postgres reachable, so starter tests ran, none skipped). `ruff check` clean. No secret values were read or printed.

## Verdict: FAIL (1 High)

One High finding (unbounded frame extraction to disk). Everything else is Medium or below. The design is otherwise sound: workflow determinism, claim/settle locking, idempotent keys, compose isolation and the mutation harness hold up.

## Spec compliance summary

| Requirement | Result |
|---|---|
| Deterministic workflow id `video-{aweme}-g{gen}`, REJECT_DUPLICATE, AlreadyStarted = dispatched | Met (outbox_starter.py:94, 237-253) |
| Claim in own txn, no lock across network, settle conditional on generation and state | Met (CLAIM_SQL, `_claim`, settle SQL lines 67-82); tested by lock_timeout probes |
| Reject non-s3, wrong bucket, bad ids; does not block next row | Met (source_key, tests parametrised) |
| Backoff exponential, dead after OUTBOX_MAX_ATTEMPTS, throttle on running count | Met |
| One Worker per queue, per-queue limits, `workflows` independent of VoiceStudio | Met (workers/run.py:44-75); llm/tts have no activity yet so no Worker |
| Workflow: 2a and 2b parallel, per-activity timeout and retry, ArtifactMissing reruns producer once, fail on second | Met; D10 test and mutation present |
| Replay histories + Replayer in CI, control reorder fails | Met; histories are from fake activities (disclosed) |
| Tracing: interceptor on client (workers inherit), no pydantic-ai instrumentation in workflow, session.id | Met |
| Dockerfile: digest pin, sha256-verified weights, weights_only | Met; allowlist addition reasoned (see Low) |
| Compose: allowlist env, no new ports | Met (no `ports:` on the 3 services; existing published ports are 127.0.0.1 except pre-existing 3000, 5432, 6379, 5173 which are out of this diff) |
| Manual verification (stack up, Temporal UI, Langfuse trace) | NOT done; plan header says `status: completed` anyway (see M9) |

Principles check: P1 holds (workflow imports only contracts + temporalio.workflow/common/exceptions + collections.abc; the three scanner allowlist additions are pure-data modules, acceptable). P2 holds (payloads carry keys; Reviewer images n/a yet). P3 holds in new code (provider errors carry name + status only; settings errors do not echo input; Langfuse keys only in exporter headers, never spans). P4 holds (all artifact keys deterministic via `work_key`, overwrite-idempotent; outbox keyed by aweme_id+generation). P6: 26 mutations + 2 controls registered, EXPECT updated.

## High

### H1. detect_subtitles writes every sampled frame to disk before processing, on an unlimited queue
- Files: src/localization/activities/ffmpeg.py:137-157 (`extract_frames` runs ffmpeg to completion, returns the full list), src/localization/activities/detect_subtitles.py:96-107 (frames deleted only after each OCR batch), src/localization/workers/run.py:69-71 (`io` queue has no limit, default 100).
- Scenario: a 1080x1920 PNG is roughly 2 to 4 MB. A 3 min video at 2 fps is 360 frames (about 1 GB); a 10 min video is about 3.5 GB. MAX_OPEN_WORKFLOWS=10 workflows can all be inside detect_subtitles at once, so 10 to 35 GB lands in the container `/tmp` (overlay filesystem, no size cap) before the first OCR call returns. Disk fills, ffmpeg and `put_json` fail, other activities on the host (including ingest/Demucs temp dirs on a shared worker host) fail with ENOSPC. The batch-wise `unlink` gives a false sense of boundedness.
- Fix: stream frames. Run ffmpeg per window (`-ss/-t`, or `-vf fps=2` piped via `image2pipe`/`-f image2` with a bounded consumer), or write JPEG and downscale to the OCR model's input size (`-q:v 3 -vf fps=2,scale=...`) and extract in chunks of N batches. Also give `io` a concurrency limit (an `IO_CONCURRENCY` setting) or move OCR to `llm`/a dedicated queue, and mount a sized tmpfs/volume for `/tmp`. Add a test that peak frames on disk never exceeds one chunk.

## Medium

### M1. Starter voice validation fails open when VOICESTUDIO_URL is unset, and ignores VOICE_PROVIDER
- outbox_starter.py:141-147 (`_catalog_for` returns None when no URL), 185-186; compose passes `VOICESTUDIO_URL: ${VOICESTUDIO_URL:-}`.
- Scenario A: URL empty (the compose default) with VOICE_PROVIDER=voicestudio: validation is skipped and every video is dispatched with an unchecked id, which is exactly the "server silently falls back to a preset" failure the check exists to stop. Tests inject `voices=lambda s: None`, so the production default wiring is never exercised.
- Scenario B: VOICE_PROVIDER=openai_compat with VOICESTUDIO_URL set: the id is checked against VoiceStudio's list, so a valid OpenAI voice (`alloy`) is refused forever (CONFIG_ERROR loop). Contradicts "any id must work".
- Fix: when `voice_provider == "voicestudio"` and no URL, return CONFIG_ERROR; when provider is not voicestudio, skip the VoiceStudio catalog (or add a provider-specific catalog). Add a test on the real `_catalog_for` for both cases.

### M2. Worker process survives a dead Worker
- workers/run.py:98-105. `asyncio.ensure_future(w.run())` results are only observed at shutdown. If one queue's `Worker.run()` raises, `stop.wait()` keeps the process alive: compose `restart: unless-stopped` never fires, that queue (for example `workflows`) is dead, videos stall silently.
- Fix: `await asyncio.wait([*tasks, stop_task], return_when=FIRST_COMPLETED)`; if a worker task finished, log its exception, shut the rest down and exit non-zero so compose restarts it. Same for the health task (log, do not kill).

### M3. ingest heartbeat gap during upload; Demucs timeout shorter than the activity budget
- ingest.py:46-51: the two `put_file` uploads use bare `asyncio.to_thread`, no heartbeat, while the workflow sets `heartbeat_timeout=2 min` (video_localization.py:49-52). Two stems of a long video over a slow link can exceed 2 min, so the attempt is killed and retried (3 attempts, then workflow fails). Wrap them in `run_blocking`.
- providers/demucs.py:28: `timeout_seconds=600` against `start_to_close=45 min` and the plan's "about 1x duration on CPU": a video longer than about 8 to 10 min times out (TransientProviderError) on each of 3 attempts, 30 min wasted, then fails. Make the timeout derive from probed duration (for example `max(600, 2.5 * duration)`).
- `run_blocking` also cannot stop its thread on cancellation: the Demucs child keeps running after the activity is cancelled or times out, so MEDIA_CONCURRENCY=2 can be exceeded by orphaned processes. Use `Popen` and kill on cancellation.

### M4. Whole vocals.wav sent to STT as one request
- transcribe.py:80-82. Demucs stems are 44.1 kHz stereo (about 10 MB/min). Many OpenAI-compatible servers cap uploads at 25 MB (about 2.5 min); the provider's 120 s HTTP timeout also bounds long audio. A 413 is classified Permanent, so the workflow fails. Not verified against VoiceStudio's limits (unknown); downmix to 16 kHz mono (`ffmpeg -ac 1 -ar 16000`) before upload costs nothing and cuts size about 10x.

### M5. Two-starter test does not prove the starters overlapped; SKIP LOCKED is not mutation-covered
- tests/starter/test_outbox_starter.py:116-132, tests/mutations/phase-04.sh (no mutation of CLAIM_SQL). If starter `a` drains all six rows before `b` claims anything, the test passes without ever contending. Postgres does not re-evaluate the `aweme_id = (subselect)` after a lock wait, so removing `FOR UPDATE SKIP LOCKED` would hand the same row to both starters; only the Temporal REJECT_DUPLICATE masks it, and the test cannot tell.
- Fix: record per-starter claimed ids and assert both claimed at least one and the sets are disjoint (or gate both on a barrier inside `_claim`). Add mutation "claim drops FOR UPDATE SKIP LOCKED" expecting that test red.

### M6. Throttle is non-atomic and the loop does not pause after a dispatch
- outbox_starter.py:176-178, 262-273. After DISPATCHED the loop immediately re-counts. With advanced visibility lagging (Elasticsearch) or two starters, the count under-reports and MAX_OPEN_WORKFLOWS is overshot, which matters because the throttle is the only protection for VoiceStudio/CPU. The test papers over it with a poll loop ("visibility is eventually consistent").
- Fix: track locally the number started in the last few seconds and add it to the count, or sleep about 1 s after each DISPATCHED.

### M7. No graceful shutdown for activities
- workers/run.py:46-52, compose (no `stop_grace_period`). `graceful_shutdown_timeout` defaults to 0 and docker stops at 10 s: every deploy kills an in-flight Demucs/STT, and each kill burns one of ingest's 3 attempts (heartbeat timeout). Set `graceful_shutdown_timeout` (for example 60 s) and `stop_grace_period`; plan already says drain before deploy, so also document it.

### M8. Dead-lettering on systemic start failures
- outbox_starter.py:76-82, 262-273. FAILED has no pause and backoff is 30+60+120+240 s, so a start-specific systemic failure (namespace missing, codec rejecting the payload, permission) kills every queued row in about 7.5 min, then needs manual SQL. This follows the plan's literal text, so it is Medium by design: classify RPC UNAVAILABLE/DEADLINE_EXCEEDED as "do not count" (like the pre-claim count_workflows failure already is), or pause the loop after N consecutive FAILED.

### M9. Plan marked completed with the manual gate and ops steps outstanding
- phase-04 frontmatter `status: completed` while "Thu cong" is unrun (no VOICE_ID/STT_MODEL/LOC_DATABASE_URL, so `worker-agents` and `localization-starter` have never started), the grants migration and the outbox table are not applied to `reup`, and replay histories come from fake activities. Honestly disclosed in the Progress section, but status should read in-progress/blocked until those pass. Replay histories should be re-recorded from a real provider run before phase 05 reorders anything.

## Low

- L1. Grants are table-wide (migrations/261026-outbox-starter-grants.sql:12-13): starter can `UPDATE` any outbox column (including `generation`, which would restart videos) and read every `videos` column. Use column grants: `GRANT SELECT (aweme_id, sec_user_id, storage_path) ON videos`, `GRANT UPDATE (state, next_attempt_at, dispatch_attempts, last_error, dispatched_at, workflow_id) ON localization_outbox`, plus SELECT on the outbox columns the claim reads. test_grants.py then pins it.
- L2. Region/cue logic (detect_subtitles.py:44-60): bands are fixed tenths of frame height, so a subtitle straddling a band edge splits detections across two bands; the winner's union misses frames from the neighbour and the cover painted later may not hide the text. Merge adjacent bands or cluster on centre with tolerance.
- L3. `ingest` fails every silent video with permanent `no_audio_track` (ingest.py:35-36, ffmpeg.py:133-134). Not in the spec (spec only names no_video_stream); confirm with product that silent videos should fail rather than be skipped to captions-only.
- L4. Image supply chain: `uv pip install torch demucs` (Dockerfile:33-37) is outside uv.lock, so demucs transitive dependencies float at build time and the CPU index is an extra index with higher precedence than PyPI. Add a hashed requirements file. `curl` stays in the runtime image (Dockerfile:14); remove it after the weights step or use a build stage. No HEALTHCHECK.
- L5. demucs_safe allowlist: acceptable and reasoned, bounded by the sha256-pinned checkpoint. `_codecs.encode` is the only callable beyond classes; it is reachable with attacker-chosen arguments only if the pinned file were swapped, which the build check prevents. `test_the_only_the_globals...` asserts fake class names (tests.test_demucs_safe.*), so it cannot detect the real set drifting; the real-image run in the progress notes is the real evidence. Consider asserting names on the real pickle in an image-gated test.
- L6. Starter env additions beyond section 6 (VOICESTUDIO_URL, VOICESTUDIO_API_KEY, VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS): justified and enforced by the compose allowlist, but plan.md section 6 table still lists them for worker-agents only; update it. Starter now depends on VoiceStudio availability (CONFIG_ERROR pause) which the plan deliberately avoided for workers; acceptable, but a hung provider blocks the starter up to `provider_timeout_seconds` (120 s) per cache miss.
- L7. `dispatch_attempts` is not reset when an operator bumps `generation`, so a re-run inherits the previous generation's failure budget (outbox_starter.py settle SQL). Reset in the bump procedure, or key attempts by generation.
- L8. `_settle` (video_localization.py:110-124): when one stage fails non-regenerably, the sibling activity is left running and un-awaited (wasted OCR/STT spend until it finishes); cancel the sibling before re-raising. `test_the_workflow_names_only_activities...` matches `'"name"' in source`, a weak substring check.
- L9. `persist_json(key, obj: Any)` can overwrite any `work/` artifact (for example vocals.wav) with JSON; restrict to `work/{aweme}/` plus `.json` suffix. Unused by the workflow today.
- L10. Each `run_once` opens a new Postgres connection and the loop reloads Settings every 2 s; harmless at current scale, use a persistent connection with reconnect.

## Mutation and test quality

Load-bearing (verified by reading the mutations against the asserting tests): deterministic id, generation in id, throttle, s3/bucket/source-prefix rejection, dead, backoff, claim-lock-across-network (the `NOWAIT` probe really conflicts), settle-by-generation, voice-must-exist (both starter and catalog), concurrency (with control at limit 3 and exact `== limit` assertion), replay reorder (with patched() control and a reordered workflow that must fail), once-per-stage rerun, 2a || 2b overlap (asserted from timestamps), image digest/sha256/weights_only, grants. Outcome-based assertions dominate (row state, workflow counts, input read back from history). Controls name the mechanism. Gaps: SKIP LOCKED (M5), AC5 "voice read at start" has a test but no mutation, `llm` queue limit untestable until it has an activity, no test exercises the real Demucs path (manual only), no test that heartbeats actually fire.

## Positive observations

- Settle statements guard on generation and state, with a test that bumps generation mid-start.
- `ArtifactMissingError` carries the producing stage as detail, and the workflow's regeneration set lives in workflow state (replay-safe).
- Child process env is an explicit allowlist in ffmpeg.py and demucs.py; errors never echo provider bodies.
- Replay test includes a negative control, and the README-level comment instructs `workflow.patched`.
- Compose adds no published ports; per-service env allowlist is enforced by test and by 3 compose mutations.

## Unresolved questions

1. Does VoiceStudio cap STT upload size or duration (affects M4)?
2. Should silent videos fail the workflow (L3)?
3. Is the `io` queue meant to carry OCR traffic unbounded, or should OCR use `llm` concurrency (OpenRouter rate limits)? Today up to 10 x 8 concurrent vision calls.

Status: DONE_WITH_CONCERNS
