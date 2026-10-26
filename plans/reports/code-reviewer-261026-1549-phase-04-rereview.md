# Phase 04 re-review after fixes

Range d939825..HEAD (15d3741 original, 61c4d16 fixes). Protocol: manual (skill not invoked; review-only, code read, tests and mutations run, no code changed).

Verified by running:
- `uv run pytest` in services/localization: 345 passed, 3 skipped (the three skips are unimplemented later-phase activities), `ruff check` clean.
- `bash scripts/run-mutations.sh 'localization/*'`: phase-03 11/11, phase-04 42/42 caught, 2/2 controls green, tree unchanged. The `downloader:infra` harness (needs the compose stack) was not run; its two new mutations were read only.
- Own experiment: ffmpeg 8.1.2, 37.3 s video at 29.97 fps, GOP 90. `extract_frames` in 16-frame chunks produced 75 frames, byte-identical (md5) to one-shot extraction. No drift, no duplicate or missing frame at chunk boundaries.
- Own mutation (in a scratch copy): replacing `os.killpg(...)` with `process.kill()` leaves `test_demucs_runtime.py` green (see N3).

## Verdict: PASS

No unresolved Critical or High. H1 is resolved, M1 to M9 are resolved or acceptably mitigated. New findings are Medium and Low.

## Verification of claimed fixes

| Item | Result | Evidence |
|---|---|---|
| H1 unbounded frames | Fixed | Chunks of 32 JPEGs at <=1024 px (about 50 KB each), seek per chunk, `io` limit 2, sized tmpfs. Test counts frames on disk at each OCR call (max <= chunk, and > 1 so the watcher is not blind) and that the scratch dir is empty after. Mutation `count=10**9` red. Chunk alignment verified byte for byte (above). |
| Bbox rescale | Correct | `to_source` scales by source/scaled per axis, clamps x,y to source-1 and w,h into the frame. Test uses 2048x1152 source, 1024x576 model view, expects exactly doubled box; mutation `factor = 1.0` red. |
| M1 voice wiring | Fixed | `_catalog_for` raises ConfigError (caught in `run_once`, outcome CONFIG_ERROR) for voicestudio without URL, returns None for other providers. Tests call the real default wiring, not the injected stub; both mutations red. |
| M2 supervise | Fixed | FIRST_COMPLETED over workers and stop, dead worker returns 1, rest shut down; `sys.exit(main(...))` propagates. Mutation red, control (clean stop = 0) green. See N1 for a shutdown-time flaw. |
| M3 heartbeat, timeout, kill | Fixed | Stems upload under `run_blocking`; Demucs timeout `max(600, 2.5 x duration)`; `Popen` in its own session, `killpg` in `finally`, cancel event set from `run_blocking`. Cancel test proves the process dies; mutation red. Group-kill itself is unguarded (N3). |
| M4 STT downmix | Fixed | 16 kHz mono via ffmpeg; test probes the format of what STT receives. 16 kHz mono 16-bit is about 1.9 MB/min, so a 25 MB cap is hit at about 13 min. Still unverified against VoiceStudio (plan discloses). |
| M5 SKIP LOCKED | Fixed | Barrier on first claim, asserts both starters claimed, sets disjoint and union equals the ids. Mutation removing `FOR UPDATE SKIP LOCKED` red. |
| M6 throttle | Mitigated | In-process `_recent` window of 3 s; test with a count that always says 0 holds the limit at 2; mutation red. Single-process only (N5). |
| M7 graceful shutdown | Partly | `graceful_shutdown_timeout` 60 s, `stop_grace_period` 90 s, test proves a running activity completes. Sequential shutdown breaks the budget when two workers drain (N1). |
| M8 outage handling | Fixed for outages | Systemic RPC statuses and OSError release the row without counting. Poison-row loop analysed in N4. |
| M9 plan status | Fixed | phase-04 `status: in-progress`, plan.md banner updated, honest "Tiến độ" with the three open items. |
| L1 column grants | Fixed | Claim, settle and video statements run under `SET ROLE localization`; `generation` UPDATE and `desc`/`play_addr` reads refused; two mutations red. `RELEASED_SQL` is not in the grants test (N7). |
| L6 plan section 6 | Fixed | Table updated for starter and `IO_CONCURRENCY`. |
| L9 persist keys | Fixed | Regex `work/<digits>/....json`, fullmatch; both mutations red. |

## New findings

### N1 (Medium). Sequential `worker.shutdown()` makes the 90 s `stop_grace_period` too short
workers/run.py:128-133 (`for worker in workers: await worker.shutdown()`).
Scenario: worker-agents runs the `workflows`, `io` and `stt` workers. Each `shutdown()` waits up to 60 s for its activities before cancelling. With a detect_subtitles on `io` and a transcribe on `stt` in flight, worker 1 consumes 60 s, worker 2 starts its 60 s at t=60, and docker SIGKILLs at 90 s: the stt activity is killed at 30 s of its grace, burning a retry attempt, which is the M7 failure again for any deploy that catches two queues busy. (The `workflows` worker is first in order, so workflow tasks also stop being served while activities drain.)
Fix: `await asyncio.gather(*(w.shutdown() for w in workers), return_exceptions=True)` so the budgets run in parallel; add a test with two workers whose `shutdown` records a start time and assert overlap. Note also that 60 s is a grace for short activities only: a Demucs run is cancelled at 60 s regardless, so the retry is still spent on a deploy during a long separation; document drain-before-deploy.

### N2 (Low). End-of-video detection can fail a valid video
ffmpeg.py `extract_frames` / detect_subtitles.py `at_end`. Seeking past EOF exits 234 with no files (reproduced). It is tolerated only when `len(boxes)*500 >= duration_ms - 1000`. If the video yields an exact multiple of 32 frames and the container duration exceeds the video stream by more than 1 s (longer audio track), the final empty chunk raises `PermanentProviderError` and the whole video fails. Rough rate: about 3% (multiple of 32) times the share of files with a long audio tail; small but non-zero.
Fix: for a chunk after the first, "non-zero exit and zero frames" is EOF whatever the duration says; drop `at_end` and the duration comparison.

### N3 (Low, test gap). Process-group kill is not asserted
tests/providers/test_demucs_runtime.py. The fake demucs is a bash script that runs `sleep`; the test checks only the bash pid. Replacing `killpg` with `process.kill()` (bash only, `sleep` orphaned) stays green (reproduced in a scratch copy), so the "demucs spawns ffmpeg and workers" guarantee that motivated `start_new_session` is unguarded. Fix: have the script start a background child, write its pid, and assert both pids are gone; add the `process.kill()` mutation.

### N4 (Low). Row-specific "systemic" failures retry forever and stall the starter
outbox_starter.py `SYSTEMIC_STATUSES`, `RELEASED_SQL`, `pauses[SYSTEMIC]`. Released rows get a fixed 30 s delay, are never counted and never dead-lettered. For a real outage that is the intent. A poison row that always returns DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED or PERMISSION_DENIED for its own reasons loops forever, and each visit sleeps the whole loop 30 s (rows with older `next_attempt_at` go first, but under steady arrivals the poison row is claimed every 30 s and blocks the starter about half the time). Payload and id problems return INVALID_ARGUMENT and still dead-letter, so this needs an unusual server behaviour. Fix: do not sleep after the first SYSTEMIC; sleep only after N consecutive SYSTEMIC outcomes, and keep a separate (large) systemic counter or age cap (for example dead after 24 h of systemic failures). The last_error column does make it visible.

### N5 (Low). Throttle protection is per process
`_recent` knows only this process's starts. With two starters and visibility lag over 3 s, each under-reports the other's starts and the limit is overshot by up to the lag's worth of starts. Consequence is more queued workflows (worker queue limits still cap VoiceStudio and CPU), not overload. Plan text discloses it. Acceptable; revisit if a second starter is ever deployed. Also `RECENT_START_SECONDS=3` is a guess about visibility lag; with Postgres or Elasticsearch visibility confirm the real lag.

### N6 (Low). tmpfs sizing: workable, with two caveats
- worker-agents `/tmp` 512 MB: each `detect_subtitles` downloads the whole source video there, x `IO_CONCURRENCY` 2, plus `transcribe` (STT limit 1) writes the vocals stem (about 10 MB/min) and the 16 kHz copy. Fine for typical short videos; a 10 to 15 min 1080p source (100 to 300 MB) x2 plus a stem can exceed 512 MB. ENOSPC is an unclassified `OSError`, retried up to the workflow's policy, then the video fails.
- worker-media `/tmp` 4 GB is ample (about 0.5 GB per 10 min ingest, x2) but tmpfs is RAM; the compose comment "MEDIA_CONCURRENCY bounds its memory" understates that two Demucs processes plus up to 4 GB of tmpfs share an unbounded host (no `mem_limit` on these services).
Fix: raise agents to 1 GB or stream the source download to a size-checked path and classify ENOSPC as transient; set `mem_limit` for worker-media.

### N7 (Low). Smaller items
- `RELEASED_SQL` is not run in `test_the_starter_can_run_every_statement` (grants test); it only uses already-granted columns today, but a future column-grant edit would not be caught. Add it.
- Heartbeat gap in detect_subtitles: heartbeat is sent after each OCR batch; a batch of 8 concurrent requests can take the provider timeout (120 s) which equals `heartbeat_timeout` 2 min (video_localization.py:49-52), so a slow provider kills the attempt before the provider timeout fires. Pre-existing; fix by heartbeating from a ticker during the gather or giving the activity a 5 min heartbeat.
- Downscaling to 1024 px and JPEG q4 changes OCR input versus the full-size PNG the plan assumed; G5 (not yet run) must be measured at this size, otherwise the bbox accuracy gate validates a different pipeline. `scale=W:H` also squashes videos with rotation metadata (ffprobe reports coded size); rotation was never handled and is now just more visible.
- `persist_json` shares the `io` queue (limit 2) with the long detect_subtitles; once phase 05+ uses it, small writes can wait behind two OCR jobs. Give it its own queue or limit when it gets a caller.
- A thread that finishes after cancellation can log "Task exception was never retrieved" (`run_blocking` abandons its task). Noise only.

## Mutation quality (new ones)

Load-bearing, verified by reading the asserting test against the mutation: disk chunking (frame count at the moment of each OCR read), rescale (exact doubled box), io limit (existing overlap test with the exact limit), voice wiring (default constructor, no stub), supervise (exit code and sibling shutdown), grace period (activity must complete), upload heartbeat (counts beats only after the separator returned), Demucs timeout (1.5 s of work against 0.4 s floor needs the media-scaled allowance), cancel kills process (pid gone), STT downmix (ffprobe of the bytes received), SKIP LOCKED (both starters claim, disjoint), throttle (stale count of 0), outage (state, attempts 0, last_error), persist regex. Gaps: process-group kill (N3), RELEASED_SQL grants (N7), parallel shutdown (N1), `at_end` edge (N2), systemic-loop bound (N4).

## Skipped Lows: judgement

- L2 (band straddling): acceptable to defer, it needs real OCR output. Keep it tied to G5 and do not close the phase without a look at real detections.
- L3 (silent videos fail permanently): acceptable pending a product answer, but it remains an open decision for the user; the spec names only no_video_stream.
- L4 (floating demucs deps, `curl` in runtime image, no HEALTHCHECK): acceptable for development, must be fixed before the image is deployed anywhere real. Not a phase-04 blocker.
- L5, L10: acceptable.
- L7 (attempts not reset on generation bump): sound reasoning, no bump procedure exists in code; make it a requirement of the procedure when written.
- L8 (sibling not cancelled): the stated reason (changes the failure-path command stream) is weaker than it sounds. No production histories exist and the replay histories are fakes to be re-recorded, so changing it now costs nothing; after the first real run it needs `workflow.patched`. Recommend doing it before phase 05 reorders anything.

## Positive observations
- Every fix came with a test asserting the outcome and a mutation; the 42/42 run shows no hollow guard.
- The frame-boundary test (20 s video, chunk 8, six ffmpeg calls including the empty final one) exercises the EOF path for real, and chunk alignment is byte-exact.
- Column grants are tested by running the actual statements under `SET ROLE`.
- The M8 fix keeps the plan's dead-lettering for row-specific failures while sparing rows during outages.

## Unresolved questions
1. VoiceStudio STT upload cap (M4 residual).
2. Silent videos: fail or caption-only (L3).
3. Real visibility lag, to size `RECENT_START_SECONDS`.

Status: DONE_WITH_CONCERNS
Summary: PASS. H1 and M1 to M9 are resolved and all 42 phase-04 mutations are caught. New items are Medium/Low, chiefly sequential worker shutdown exceeding the 90 s stop grace (N1).
