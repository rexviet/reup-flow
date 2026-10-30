# Code review: phase 06 dubbing (ed723a9..fd7888e)

Branch `feat/localization-phase-00-infra`, 53 files, reviewed against `phase-06-dubbing.md`, `plan.md` §0 (principles 4, 5, 6), §5, §6, AC1, AC6. Skill `backend-ai-toolkit:code-review` was invoked; stages run: spec compliance, quality, adversarial. Review only: no source changed, nothing committed.

## Verdict: PASS (no Critical, no High). 6 Medium, 9 Low.

The three things the phase exists to prove hold: a TTS hash reaches the provider once across a real SIGKILL (including between answer and write), the Dubber's speaking is pinned to the `tts` queue and counted by its limit, and the stage is behind a replay marker that older histories survive. The Mediums are real defects or spec gaps, none breaks AC1/AC6 on the paths the plan names.

## Evidence run

| Check | Result |
|---|---|
| `cd services/localization && uv run pytest -q` | 693 passed, 1 skipped, 6 deselected (223 s). Matches the phase file's claim. |
| `uv run ruff check` and `ruff format --check` | clean |
| `bash tests/mutations/phase-06.sh` | all 54 mutations caught (each by the named tests), 3 controls stayed green, source tree clean afterwards. No summary line is printed by the harness; counted from the log. |
| Four re-anchored older mutations (04 `_prepare` cancel, 05 model unbound, two `QueueSpec`) | Diff read only, not run (04/05 harnesses are very long). Anchors now name the `_prepare` loop / `factory is None` / the new queue spec, which are the right sites. |
| ffmpeg filter graph | Reproduced by hand with ffmpeg 8.1.2 (see M2, L1). The worker image uses 5.1; the `amix normalize=0` option and graph were not re-run there by me. |
| Live behaviour | Not run and not claimable: no VoiceStudio, no Vietnamese voice. The phase file says so honestly. |

A Temporal dev server (pid 27944, 5 h old, not mine) is running; left alone. No process of mine remains.

## Stage 1: spec compliance

Matches: hash fields (provider, model, engine, voice_id, settings, text) in `synthesize.py:67-80`; dedupe before batching (`plan_synthesis`, `segment_hashes`); batch of 10, heartbeat per hash plus background; stretch table (`stretch_for`); Dubber tools and queues; clamp via `ModelRetry`; 3 rewrites; `script.v{n+1}` never edits the old; `dub-segments.json` for every segment; ducking, two-pass loudnorm, ±50 ms length rule; env contract (`.env.example`, compose, allowlist); harness registered in `scripts/run-mutations.sh` in the same commit (+54, +3).

Declared deviations, assessed:
- Activities for `write_dubbed_script`, `plan_synthesis`, `commit_dub`: justified (workflow holds no script content; payloads carry hashes and keys only). Accepted.
- Script after Dubber is always version 2: consistent (`DubbedAudio.script_version`), accepted. Later `dub` reruns give v3+ via `script.version + 1`.
- `accept` only validates: accepted, because `final_stretch` recomputes from measured length, so no state lives outside the conversation.
- Hyphenated metadata names: accepted for the code, but the later-phase docs were not updated (M5).
- Late bug fix (failed batch acted on at once, siblings cancelled): present in `_synthesize` and covered by two tests plus two mutations.

## Findings

### Medium

**M1. A dubbing-time regeneration returns a stale `script` and `prep`.**
`workflows/video_localization.py:173-176` with `:307-323`. `_dub_with_regeneration` rebinds `prep, script = await self._localize(...)` only as locals; `run()` returns `LocalizationResult(prep=prep, script=script, dub=dub)` using the pre-regeneration values.
Scenario: `script.v1.json` goes missing, the Localizer reruns (LLM, non-deterministic) and rewrites the same key; this time it produces a line with a domain the source lacks, so `needs_human=True` and the flagged ids differ. The result still carries the first run's `needs_human=False`. Phase 08 reads the safety flag from this result, so the safety gate can be bypassed. Same for a transcript regenerated inside `_localize`.
Fix: have `_dub_with_regeneration` return `(prep, script, dub)` (or store them on `self`) and build the result from those. Add a test in `test_dubbing_stage.py` that regenerates with a flagged second output and asserts the result's `needs_human`.

**M2. A voice that runs past the end of the video fails the whole run; the spec's own degrade path ("accept at 1.15, mark `overflow_unresolved`") ends in a hard failure for tail lines.**
`activities/assemble_audio.py:63-66` (voice is `apad`ed to the video length, never `atrim`med) and `:143-150`; pinned as intended by `tests/activities/test_assemble_audio.py:215`.
Reproduced: music 10 s, a 3 s clip at 8.5 s with stretch 1.15 gives a 11.108 s mix. `AudioLengthMismatch` is non-retryable, so the run dies after all TTS and Dubber money is spent. A last segment whose slot ends at the video end and which still overflows at 1.15 does exactly this; Vietnamese is usually longer than Chinese, and closing lines ("thanks for watching") are common.
Fix: `atrim=end={seconds}` on `[voice]` (and keep the ±50 ms check as the guard for real bugs), or clamp the last clip with a short fade. Keep `overflow_unresolved` as the signal. Update the test to expect a correctly sized track and the flag.

**M3. Two concurrent calls for one hash both reach the provider; AC1 is stated absolutely.**
`activities/synthesize.py:177-208` (`clip`: cache check, then provider, no single-flight). Exposure:
- the Dubber issuing parallel `synthesize` calls (different ids, same rewritten text) on the `tts` queue, limit 2;
- up to `MAX_OPEN_WORKFLOWS=10` runs speaking the same stock phrase at the same time (the cache is global, `cache/tts/`);
- a cancelled batch (`_synthesize` `handle.cancel()`, default TRY_CANCEL, seen only at the next 5 s heartbeat) overlapping the restarted stage after a regeneration.
AC1's only declared exception is the kill window. The batch dedupe test covers the plan, not these.
Fix: per-process single-flight (`dict[hash, asyncio.Task]`) inside `Synthesizer`; for the cancel case use `cancellation_type=WAIT_CANCELLATION_COMPLETED` on `SYNTHESIZE`. Cross-process overlap can be documented as a cost-only exception in AC1.

**M4. The Dubber's run caps do not scale with the work, and a Dubber failure has no degrade path.**
`agents/temporal.py` (`DUBBER_USAGE_LIMITS = request_limit=120, tool_calls_limit=400`) against up to about 5 tool calls and 3 model requests per unfit line (`get_segment`, up to 3 `synthesize`, `accept`). Around 30 or more unfit lines can exhaust it, raising a usage-limit error that fails the workflow (after TTS spend) deterministically on every new generation.
Fix: derive limits from `len(pending_ids)` (for example `8 * n + 20`), or on `UsageLimitExceeded` fall back to empty decisions so lines play at 1.15 with `overflow_unresolved`. At minimum record the ceiling in the phase file.

**M5. Later-phase text still names the metadata key `text_vi`; code writes `text-vi`.**
`plan.md:117, 305` (AC2), `phase-08-reviewer-and-publish.md:75`, `phase-10-e2e-and-docs.md:44`. Only phase 06's own file records the hyphen. The AC2 test written from those lines reads the wrong key and finds nothing (or, with a lenient helper, passes vacuously).
Fix: update those four lines to `text-vi` (and say that the name is a constant `META_TEXT` in `synthesize.py`). The mutation "metadata names are renamed" already pins the code side.

**M6. `needs_human` gating is specified only as an end state; the dubbing cost and the data path are not decided.**
`phase-08-reviewer-and-publish.md:62` says the phase-05 safety flag leads to `needs_human` and `:65` that nothing goes to `output/`. It does not say whether a flagged run still dubs and renders (as now), nor where the flag is read from (it exists only in `ScriptResult`, not in the stored script, so M1 matters). Flagged lines, including a foreign domain, are voiced and kept forever in the global `cache/tts/` (no lifecycle). Separately, `apply_decisions` (`dub_script.py:56-66`) drops `refused` silently (`_refused` is unused), so a Dubber rewrite that tried to add a contact (a strong injection signal) leaves no trace.
Fix: decide in phase 08, in writing: either skip dub and render when `needs_human`, or accept the spend and state that the review copy is the product; read the flag from `LocalizationResult.script`; log or span-tag refused rewrites. Phase 06 recorded the limit honestly, so this is a spec gap and not a code defect.

### Low

- **L1. True-peak ceiling is untested and `TRUE_PEAK_DBTP` wiring is unmutated.** Tests assert LUFS ±1 only (`test_assemble_audio.py:147`); the media-worker mutation covers loudness, not peak. My manual run (two overlapping 0.9-amplitude spiky clips over pink noise, same filter graph, two-pass loudnorm, `-ar 44100`) gave -14.0 LUFS and a true peak of -5.0 dBFS, so the design holds, but a regression would pass. Add a true-peak assertion and a mutation that drops `TP`.
- **L2. `TextTooLong` fails the whole stage** for lines over about 1050 UTF-8 bytes (`synthesize.py:35-36, 183-188`). Rare for subtitles, but non-retryable and it also hits a Dubber rewrite. Acceptable; note in runbook.
- **L3. Dubber can slow a fitting clip.** `final_stretch` (`synthesize.py:100-106`) honours a chosen ratio below 1 (down to 0.85) even when the clip already fits; the phase table says fitting clips are 1.0. The result still fits the slot, only slower. Cosmetic.
- **L4. Ducking threshold is absolute** (`assemble_audio.py:42`, 0.03 linear) on the pre-normalisation voice, so a quiet TTS level would barely duck. Tone tests cannot show this; it is correctly listed as unverified. Real-voice check needed before closing the phase.
- **L5. Replay histories come from stand-ins** (disclosed). They do contain the real durable-agent activity names (`agent__dubber__model_request` on `llm`, `agent__dubber__toolset__<agent>__call_tool` on `tts`), which is better than faked names, but no history exercises `get_segment` on `io`.
- **L6. `worker-agents` now refuses to start with an empty `DUBBER_MODEL`** (`workers/run.py` `model_for("dubber")`), as it already did for `LOCALIZER_MODEL`. Deploy note: a `.env` copied from before this commit will crash-loop the worker.
- **L7. `run_ffmpeg` treats every non-zero exit as permanent** (`ffmpeg.py` new function), including an OOM-kill. Retry would help on a signal exit.
- **L8. Zero-length slot** (`slot_ms=0`, allowed by the model) is always "unfit"; the Dubber would spend 3 rewrites on it. Filter such segments out of `unfit_ids` or give them stretch 1.15 directly.
- **L9. `synthesize_segments` returns a list of `SynthClip` that the workflow ignores** (`video_localization.py:389-394`). Harmless, but the payload grows with batch size for nothing.

## Focus-area answers

- **Cache keying:** complete and mutation-checked per field (voice id, settings, text, model, engine each have a red mutation; one shared test `test_every_part_of_how_a_clip_sounds_changes_its_hash`). Provider payload (`openai_tts.py`) is fully determined by those fields; the provider rejects non-WAV so junk is never cached. Engine is read once per run (`voice_profile`) and recorded in history; a mid-run engine switch in VoiceStudio would mis-key clips (inherent, low).
- **Dedupe across batches:** done in `plan_synthesis`; test uses a slow provider so two batches really overlap on the repeated text.
- **SIGKILL between answer and write:** tested with real processes, `os.kill` in `before_put`, a directory-backed bucket and an append-only call log; the test lists the in-flight hashes and requires `repeated == in_flight` with `1 <= len <= TTS_CONCURRENCY`. Strong, outcome-based. The write is a single PUT, so a clip is whole or absent.
- **Determinism and replay:** marker placed after the Localizer marker; phase-04/05 histories replay, a control (`UngatedDubWorkflow`) proves the marker is what saves them, and the phase-06 set covers Dubber, lost stem, lost script. Sound. Only M1 is a state bug, not a determinism one.
- **Bytes in payloads:** none; hashes, keys, small decisions only.
- **Dubber pinning and untrusted text:** `synthesize` is registered on the `tts` queue, asserted by name and by measured peak overlap (limit 2 gives peak 2; control limit 4 gives 4). Source and Vietnamese text go through `wrap_untrusted` (angle brackets escaped). Output is checked (ids complete, unique, non-empty), a rewrite that adds a contact is refused (see M6 for the missing signal), rewrites per segment are counted from the conversation so replay-safe.
- **Filter graph:** stretch direction correct (`atempo=duration/slot` speeds up), `adelay=start:all=1` per clip, `sidechaincompress` has bgm as signal and voice as key, voice kept for the final `amix`, `normalize=0`, loudnorm two-pass with `linear=true`, length checked. Defects: M2. Evidence is tone/noise only; real-voice LUFS and balance unmeasured (stated).
- **needs_human still dubbed:** see M6 and M1.
- **Mutation tests:** the 54 are load-bearing (each names the tests that must fail, harness checks the diff applied and the tests ran). Gaps: true peak (L1), single-flight (M3), stale result (M1).
- **Phase status:** `in-progress` with the unverified items listed is honest. One correction: the claim that the full `run-mutations.sh` is green was not re-run by me; only harness 06 was.

Status: DONE
Summary: Phase 06 passes review (no Critical or High); six Medium findings, chiefly a stale `needs_human` result after a dubbing-time regeneration (M1) and a hard failure when the last line overflows the video end (M2). Tests (693 passed) and the phase-06 mutation harness (54 caught, 3 controls green) were run; the skill `backend-ai-toolkit:code-review` was invoked.
Concerns/Blockers: live behaviour (real voice, real LUFS, Dubber with a real model) remains unverified by design; the four re-anchored 04/05 mutations were read, not executed.
