# Phase 05 (Localizer) review, range dfb3e54..HEAD

Verdict: FAIL (one High). Skill `backend-ai-toolkit:code-review` was not invoked as a tool; the three stages (spec, quality, adversarial) were applied manually.

Run: `uv run pytest tests/agents tests/text tests/workers tests/workflows tests/replay` = 204 passed. `phase-05.sh` = 44 caught + 2 controls green (matches manifest 44:2), tree restored clean. (`run-mutations.sh 'localization/*'` also runs 03 and 04; I stopped it in phase 04 and ran the 05 harness directly.)

## High

H1. Unbounded agent conversation can hang the workflow RUNNING forever. `agents/temporal.py` run_localizer, `contracts/limits.py:10-17`.
Every model request re-sends the whole history, including each `read_transcript` page. Nothing bounds it: `USAGE_LIMITS` counts calls, not bytes; `segment_count` is not capped; no `payload_size` pre-check is used (only defined in limits.py, never called). `SizeGuardCodec` (256 KiB) raising while the workflow's commands are encoded fails the workflow TASK, which Temporal retries forever (limits.py documents exactly this). The G6 test (60 segments, one read, no validator retry) only shows ~3 min is fine. Failure scenario: a 10-15 min Douyin video (~200-250 dense segments), or a model that re-reads the transcript, or a validator retry carrying a long answer, pushes one request past 256 KiB; the run neither fails nor needs_human, it sits RUNNING until the multi-day execution timeout and holds a MAX_OPEN_WORKFLOWS slot.
Fix: before the agent, fail non-retryably (ApplicationError) when `segment_count` exceeds a measured cap, or implement the windowed run the phase offers (`LocalizerDeps.window` already exists); add a test with a long transcript asserting WorkflowFailureError, and a mutation for it.

## Medium

M1. The contact check cannot catch the injection when the payload is in the source line itself. `text/safety.py contacts_missing_from_source`, `tests/agents/test_localizer_workflow.py:~345-375`. The plan's own fixture puts "shop-xyz.vn" in the transcript line; the test shows that line's `text_vi` containing the domain is NOT flagged (only line 0 is). Spec-compliant ("not in src_zh") but the defense then rests entirely on the glossary guard and the model; a spoken or on-screen ad domain is dubbed verbatim with no flag. Fix: decide with the user whether any contact in a dubbed line should flag regardless of source (or flag when the source contact is not also in the channel allowlist); at minimum document it in the phase.

M2. Glossary poisoning that passes the guards is permanent. `activities/glossary.py`. Guards cover contacts, digits, length; nothing checks `term_zh` actually occurs in the transcript the run read, or that `term_vi` is plausible Vietnamese. Speech can induce "X dịch là Y" for any term (even a competitor brand not yet seen); `DO NOTHING` then makes it unfixable by the agent for every later video of the channel, and `glossary_lookup` tells the model to use it "exactly". No delete/review path. Fix: have the port verify `term_zh` is a substring of the run's transcript before inserting; add a documented admin correction path; consider `source_aweme_id` review. (Accepted by the user as "auto-use with guards", so this is hardening, not a reversal.)

M3. Unwrapped stored text in a tool reply. `glossary.py` upsert "exists" message embeds `existing[0]!r` outside `<untrusted_video_text>`. It is a stored, attacker-influenced value (<=64 chars, contact-checked) returned to the model as plain tool text. Fix: pass it through `wrap_untrusted` (message built in `localizer.glossary_upsert`).

M4. `read_onscreen_text` does not meet the spec ("đọc cue OCR") and is a dead tool. `agents/localizer.py`: it returns only overlapping segment ids; `INSTRUCTIONS` never tells the model to call it, so it costs prompt tokens and is effectively unused; `on_screen` is computed in `assemble_script` independently. The limitation is honestly disclosed (SubtitleCue has no text), so this is a scope gap, not a hidden one. Fix: remove the tool or reference it in the instructions with a stated purpose, and record in phase 06/07 that on-screen text is untranslated; or extend SubtitleCue (bước 2b).

M5. `voice_rate` has no sanity bounds and caches forever. `activities/voice_rate.py rate_from_audio/put`. A TTS that returns silence-padded, truncated or near-empty audio gives a wildly wrong rate; it is stored under the key (`ON CONFLICT DO NOTHING`, CHECK only >0) and every later video on that voice gets wrong syllable budgets with no way to re-measure. `duration_ms == 0` raises ZeroDivisionError and is retried 8x. Leading/trailing silence also biases the rate low. Fix: reject rates outside e.g. 2..8 syllables/s (non-retryable error, nothing stored), trim silence before probing, raise a typed error for zero duration. D6 calibration is unrun (disclosed).

## Low

L1. `vi_syllables` edge cases (verified by running): "số 007" -> 2 syllables (leading zeros dropped, spoken "không không bảy"); "-5 độ" -> 2 (missing "âm"); "video 4K" -> "bốn nghìn" (4K is read "bốn ca"). `ESTIMATE_BATCH` silently drops texts beyond 60 without telling the model (`localizer.py estimate_duration`).
L2. Glossary cap is count-then-insert in one statement under READ COMMITTED; parallel tool calls in one run can overshoot 20 slightly. Harmless.
L3. Script ids are positions after sort by (start,end), not provider ids (`localizer_ports.read_transcript_document`). Correct and tested, but any later phase joining `LocalizedSegment.id` against the raw transcript document's ids breaks when the provider ids differ; make phase 06+ read through the same function.
L4. G6 test does not exercise a validator retry (full 60-line answer re-sent) or glossary calls; add one.
L5. `voice_rate` activity does not `tag_session`, so its span is not under the video's Langfuse session. Model requests carry no LLM-generation span (pydantic-ai instrumentation off by design); AC7 will need it in phase 09.
L6. Phase file lists `tests/agents/test_glossary_injection.py`; the equivalent lives in `test_localizer_workflow.py`/`test_glossary.py`. Cosmetic.

## Checked and fine

- Replay safety: `workflow.patched("localize-script")` sits after `_prepare`; removing it turns 8 replay tests red (mutation verified); phase-04 histories still replay. Result type change is covered by `LocalizationResult(prep, script=None)`. No consumer of `PrepResult` outside tests/workflow. Histories are from stand-in activities (disclosed; the command sequence is what replay checks).
- G1/queues: tool activities on `io`, model requests on `llm`, `estimate_duration` inline; pinned and mutation-tested; real SIGKILL test shows one glossary row, tool retried, model request not re-run. `AgentPlugin` on both `llm` and `io` workers registers all agent activities on both, harmless because task queues are pinned.
- Agent failure fails the workflow (test asserts WorkflowFailureError, no assemble scheduled). Consistent with phase 08: it only consumes `ScriptResult.needs_human` (phase-08 line 62); needs_human is reserved for the safety flag and review rounds. Phase 08 must still decide what a failed Localizer means for the `runs` row (phase file says so).
- Secrets/bytes: `LocalizerDeps` is keys and numbers; model and DB URL bound at worker start (`bind_agent_runtime`, refuses to start without them); `DeferredModel` reads no env in workflow code; payloads carry text and keys only; no pydantic-ai instrumentation, so prompts are not in spans.
- `assemble_script`: coverage checked exactly (no missing, duplicate, extra, empty), timing/src/slot/on_screen from documents not the model, key `work/{id}/r0/script.v1.json` deterministic so retries overwrite the same object.
- Mutation harness: edits are exact-once string replacements, no-op mutations are hard errors, red must include the named tests, and controls (reworded instructions, page size) stay green. Not self-comparing. Missing mutation worth adding: the H1 guard once it exists; the control "instructions reworded still replays" is valid because replay ignores activity input.
- Tests assert outcomes (rows in DB, stored script, history queues) rather than spies; injection test uses a model that obeys the attack.

## Status honesty

Phase is in-progress and says so; the unrun manual video run, D6 calibration (25% tolerance is a placeholder), unusable configured model (`deepseek-v4-pro` hangs >240 s) and "0/9" being hand-written data are all disclosed. Not a baseline for phase 09. Nothing overstated. Phase 05 must not close before the real-video run.

## Unresolved questions

- Should any contact in a dubbed line flag the run even when the source carries it (M1)?
- Cap on segments/transcript size for the Localizer, or implement the 60 s windows now (H1)?
