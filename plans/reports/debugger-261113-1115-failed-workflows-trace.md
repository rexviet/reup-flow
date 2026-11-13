# Failed localization workflows, 2026-11-13 morning: root cause trace

Read-only investigation. Nothing was retried, restarted, or changed. Times are Asia/Saigon (UTC+7) unless marked Z.

## Outcome

The 3 failed videos have two separate causes. Neither is related to the auto-queue triggers that were dropped at 11:10, the assemble_audio edit, or the outbox_starter edit.

| Video (aweme_id) | Failed workflow(s) | Cause | Retry would |
|---|---|---|---|
| 7696076126451518735 | g1 (08:03-08:26), g2 (08:28-09:33) | g1: STT response failed to parse (`PermanentProviderError`). g2: transcript has 181 segments, over the 150-segment Localizer cap | Fail again at the cap (deterministic) |
| 7686980699886275846 | g1 (09:51-10:14) | Transcript has 167 segments, over the 150-segment Localizer cap | Fail again at the cap (deterministic) |
| 7689374055073434880 | g1 (10:14-11:06) | STT chunk 27 returned a 200 whose segments or words fail validation; same failure 24 times in a row | Very likely fail again at chunk 27. If it got past that, it would likely hit the cap too (estimate) |

Cause A: long videos exceed `MAX_SEGMENTS = 150`. This is a fixed design limit, not a regression. It caused 2 of the failed workflows and is the final failure for 2 of the 3 videos.
Cause B: STT parse failure in `OpenAICompatStt._parse`. It caused the other 2 failed workflows.

## Failure rate

- From 07:00, when the morning queue started: 8 workflows started. 4 failed (3 distinct videos), 3 completed (status needs_human), and 1 is still running (video-7692039666727472434-g1, started 11:06). That is **4 failed out of 8 started**, or 4 out of 7 that finished. By video, **3 failed out of 6**.
- From local midnight: 10 started and 5 failed. The extra failure is video-7695227005952330363-g1 at 00:16, which failed because the Localizer gave up: "line 12: the line has characters the caption font cannot show ('반')". A g2 rerun of that video completed at 07:56, so it is not counted among the morning's 3.

Source: `temporal workflow list --query 'StartTime > "2026-11-12T17:00:00Z"'`, which returns 10 rows and matches the `localization.runs` and `dashboard_actions` rows.

## Evidence per workflow

### video-7696076126451518735-g1: STT parse failure
- History: `transcribe` failed at 01:26:03Z with `retryState=RETRY_STATE_NON_RETRYABLE_FAILURE`, `type=PermanentProviderError`, message `stt: unexpected response shape`. This was attempt 1.
- Voice worker log (`~/.pm2/logs/reup-voice-worker-error.log:6446`): `08:26:03 ... ApplicationError: PermanentProviderError: stt: unexpected response shape`. The response was a 200 (`POST http://127.0.0.1:3900/v1/audio/transcriptions "HTTP/1.1 200 OK"`).
- The voice worker was then running the committed code, where both parse branches raise this same message without detail. The process was restarted at 09:33:20, after the 09:20:26 edit to openai_stt.py. **Unverified:** which branch raised it and which field was involved.

### video-7696076126451518735-g2 (manual rerun 08:28): segment cap
- `transcribe` attempt 1 hit the 30 min StartToClose timeout (`started transcribe attempt 2 activity StartToClose timeout`, 02:18:37Z). Attempt 2 succeeded, so STT did not fail this time on the same video.
- Ingest `duration_ms=842967` (14.0 min), transcribe `segment_count=158`, complete_transcript `segment_count=181`.
- WF FAILED 02:33:11Z: `the transcript has 181 segments, over the 150 the Localizer reads in one piece`. The error is raised at `services/localization/src/localization/agents/temporal.py:306-312` (`MAX_SEGMENTS = 150` at :202), with `PayloadTooLarge` and non-retryable. Cost: 65 min of compute and $0.132 of LLM spend before the check ran.

### video-7686980699886275846-g1: segment cap
- Ingest `duration_ms=839809` (14.0 min), transcribe 148 segments, complete_transcript 167 segments.
- WF FAILED 03:14:06Z: `the transcript has 167 segments, over the 150 the Localizer reads in one piece`. Cost: 22 min and $0.156.
- `complete_transcript` reached attempt 3 after `source OCR: invalid timestamped readings`, then succeeded. That was not the cause.

### video-7689374055073434880-g1: STT parse failure, deterministic on one chunk
- History: `transcribe` ran 8 attempts (policy `maximumAttempts: 8`, `startToCloseTimeout 1800s`). It ended `RETRY_STATE_MAXIMUM_ATTEMPTS_REACHED` at 04:06:15Z with `TransientProviderError: stt: unexpected response shape (object with keys: duration,language,segments,task,text,words, unreadable segments or words)`.
- Voice worker log, every attempt: `chunk 27: stt: unexpected response shape (... unreadable segments or words); asking again (1 of 2)`, then `(2 of 2)`, then `Completing activity as failed`. This appears at 10:26:38, 10:30:57, 10:35:15, and so on until 11:06. That is 8 activity attempts x 3 chunk calls = 24 identical failures, always on **chunk 27**.
- VoiceStudio log (`~/Library/Application Support/OmniVoice/omnivoice.log:9030-9043`) shows the same 11.910 s audio each time, `Detected language 'ko' with probability 0.87`, with no error on the server side. Decoding runs at temperature 0, so the output is deterministic and the same malformed item comes back every time.
- The body has `segments` as a list, so the failure is in the per-item block at `openai_stt.py:128-156`. VoiceStudio always returns numeric `start`/`end` and string `word` (`backend/api/routers/openai_compat.py:915-964`). That leaves the pydantic contract checks (`ValidationError` is a `ValueError`) in `contracts/models.py`: `end_ms < start_ms` on a word (`:77-81`) or segment (`:100-104`), word text over 200 characters (`:73`), or a NaN time in `_ms`. **Unverified:** which one. The reproduction needed the worker's secret environment, which was blocked, and spans do not record response bodies.
- Each attempt re-transcribes chunks 0-26 before reaching chunk 27, about 4-5 min per attempt. About 44 min were spent on retries that could not succeed. The 09:20 edit to openai_stt.py:153-156 reclassified this branch from Permanent to Transient. The old code would have failed in about 5 min with the same outcome.
- Duration is 733 s (12.2 min). At the 0.20-0.21 segments/s seen in 7686 and 7696, about 150-160 segments would be expected, so the video would likely hit the cap next (estimate).

## Hypotheses eliminated

- **Dropped auto-queue triggers (about 11:10):** the last failure closed at 11:06:15, before the drop. `dashboard_actions` shows each `localize {"source": "auto_queue"}` starting the next video at the exact moment the previous run ended (09:51:42 and 10:14:05 match `runs.updated_at`). The queue only dispatches and plays no part in activity outcomes.
- **assemble_audio.py edit:** the file was changed at 11:07:30, and the worker-media image was built 2026-11-12T16:48:39Z. No failure reached assemble_audio.
- **outbox_starter.py edit:** the starter image was built 01:50Z (08:50), after the 08:46 edit, so it is live. It only dispatches workflows. The workflow inputs look normal (see the 7689 transcribe input), and the failures are in transcribe and the workflow-side cap.
- **VoiceStudio outage or infrastructure:** all STT requests returned 200, the server log shows no errors, and other videos transcribed fine in the same window.

## Recommendations (not applied)

1. **Segment cap, cause A.** `agents/temporal.py:200-202` says "a longer video needs the transcript read in windows". The long-term fix is windowed Localizer input for videos over about 12.5 min. In the meantime, fail fast or hold the video at intake or after ingest when `duration_ms` predicts more than 150 segments. Today the check at `agents/temporal.py:306` only runs after transcription, OCR and singing detection, which cost 22-65 min and $0.13-0.16 per video. Retrying 7696 or 7686 as-is will fail the same way.
2. **STT parse, cause B.**
   - In `providers/openai_stt.py:153-156`, include the validation error location (field and index, not text) in the message so the offending field is named next time.
   - Keep the item-level failure apart from the shape check at `:126-127`. A 200 with a well-formed body but invalid items is deterministic at temperature 0, so retrying it (8 activity attempts x 3 chunk calls via `transcribe.py:274-285` and `video_localization.py:157-161`) only burns time.
   - Better still, sanitize per item: drop or clamp a word whose end is before its start, and skip non-finite times. One bad word should not fail a 12-minute video.
3. **Transcribe timeout.** The 30 min StartToClose (`video_localization.py:159`) is close to the real transcription time for 14-minute videos (7696 g2 attempt 1 timed out). VoiceStudio reloads the faster-whisper model on every request ("faster-whisper loading ..." on each chunk in omnivoice.log), which adds about 1 s per chunk.
4. **Monitoring.** Alert on repeated identical activity failures on the same chunk, and track segment_count against the cap per run.

## Unresolved questions

- Which exact field failed validation for 7689 chunk 27, and for 7696 g1. To find out, reproduce with the worker environment or apply recommendation 2's error detail.
- Why the same 7696 audio failed STT in g1 but passed in g2. g2 re-ran ingest, so chunk boundaries may differ. Not verified.
