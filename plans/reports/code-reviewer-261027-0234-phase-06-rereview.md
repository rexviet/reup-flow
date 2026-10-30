# Re-review: phase 06 fixes (fd7888e..75381fe)

Branch `feat/localization-phase-00-infra`. Narrow re-review of the fixes for M1-M6 of `code-reviewer-261027-0108-phase-06-review.md` and the regressions they could cause. Skill `backend-ai-toolkit:code-review` was invoked. Review only: no source changed, nothing committed (the one temporary edit used below was restored; `git status` shows only the untracked reports).

## Verdict: PASS (no Critical, no High). 0 Medium, 5 new Low.

M1, M2, M3 are fixed as intended. No replay or determinism regression.

## Evidence run

| Check | Result |
|---|---|
| `cd services/localization && uv run pytest -q` | 702 passed, 1 skipped, 6 deselected (226 s). Matches the phase file. |
| `bash tests/mutations/phase-06.sh` | 61 mutations caught, 3 controls stayed green, exit 0, tree clean afterwards. |
| ffmpeg 8.1.2 on a synthetic overrun (see M2) | Verified by hand. |
| Single-flight removed, run the cross-batch test alone | Stays green (see M3 harness note). Source restored from a copy; `git status` clean. |
| Not run | Full `run-mutations.sh`; ffmpeg 5.1 of the worker image (I used 8.1.2); anything live. |

No process of mine remains. The Temporal dev server of another session was left alone.

## M1 (stale result after a dubbing-time regeneration): FIXED

`workflows/video_localization.py:173-177, 309-325`. `_dub_with_regeneration` now returns `(prep, script, dub)` and `run()` rebinds all three before building `LocalizationResult`. The loop passes the regenerated `prep, script` to the next `_dub`, so the track and the reported script are the same object.
- Replay: the change is a local tuple. No activity, marker, timer or command was added, removed or reordered; the `workflow.patched` calls and the call sequence are unchanged. The replay suite and the `UngatedDubWorkflow` control are green.
- Other paths: the normal path returns the arguments it was given (identical to before). There is no resume or continue-as-new path in this workflow. A localize regeneration inside the stage returns the new pair from `_localize`. An `ingest` regeneration does not update `prep.ingest` (the result of `_ingest` is discarded). That was already so and is harmless while the ingest keys are deterministic; see L-b.
- Test `test_a_script_made_again_with_a_flag_is_the_script_the_result_reports` makes the second `assemble_script` flag segment 1 and asserts the result carries it; mutation "result reports the script from before a regeneration" is red on it. Good: a real outcome assertion, and it fails if the tuple is dropped.

## M2 (mix cut at the video end): FIXED, verified

Graph (`assemble_audio.py:69-75`): `amix ... duration=longest,atrim=end=S[mix]` after the last `amix`, so it applies to the voice+ducked music sum only.
Synthetic case: 10 s pink-noise bed, a 3 s tone at 8.5 s, stretch 1.15 (ends at about 11.1 s).
- Untrimmed graph: 11.113 s. Trimmed graph: 10.000 s exactly.
- The trimmed samples are bit-identical to the untrimmed ones over the first 10 s (max abs difference 0.0). So the ducker, music, and voice are untouched; only the tail is removed.
- The cut is at the last sample of the file, so there is nothing after it to click against; last-sample amplitude 0.035 against peak 0.088.
- Full chain (pass 1 measure, pass 2 `linear=true`, `-ar 44100`) with a 0.9-amplitude click voice overrunning the end: output 10.000 s, -16.2 LUFS, peak -2.0 dBFS against the -1.5 ceiling input. The `loudnorm` result is unchanged in kind. The new true-peak test `test_a_peaky_voice_stays_under_the_true_peak_ceiling` and its `TP=0` mutation hold (caught).
- The `overflow_unresolved` flag is still set from `needed > STRETCH_MAX` in `final_stretch` and is not touched.
- Is the ±50 ms check still load-bearing? Less than before, but not dead. The graph now pads and trims both the music and the final mix to the same `duration_ms` that `check_length` compares against, so it can no longer fire from a long voice. It still guards the post-graph chain (loudnorm pass 2, resample, container) and a future edit that removes the `atrim`/`apad`. The test now calls `check_length` directly on a synthetic wrong-length WAV and the mutation of its `if` is caught. The trim does not mask a wrong mix in a way the check used to detect except for L-a below.

## M3 (single-flight per hash): FIXED, one design note

`activities/synthesize.py:178-200`.
- Deadlock: none. The request runs as its own task; callers only await a shield on it; a failing request lands in `_landed`, which always pops the map entry first.
- Leak: the entry is removed by the done callback on success, failure and cancellation, so the map cannot grow past the number of requests in flight. A request that hangs in the provider holds its slot until the provider's timeout, as it did before.
- Cancelled caller: `asyncio.shield` means the cancellation hits the waiter only; test `test_a_caller_that_gives_up_does_not_cancel_the_request_the_others_wait_for` proves the patient caller still gets the clip with one provider call; mutation `await flight` is caught.
- Failure not remembered: entry popped before the next call; test asserts a second attempt calls the provider again.
- Unobserved exception: `_landed` calls `flight.exception()` on a non-cancelled future, so no "exception never retrieved" warning when all callers were cancelled.
- AC1 and hash semantics: `hash_of` is computed in `clip` exactly as before, the cache check is unchanged and happens inside the task, the metadata and `before_put` hook order is unchanged. The SIGKILL tests are green. The single-flight also closes part of the cancelled-batch overlap the earlier review raised: the restarted stage joins the detached request instead of starting a new one.
- Design note (L-c): after an activity is cancelled the shared request keeps running detached (this is the intent, the speech is paid for and gets cached), but it is no longer tied to the activity's heartbeat or lifetime. At worker shutdown it is simply dropped; nothing is lost because the put is atomic.

Harness narrowing (mutation "plan lists a text once per segment" now expects only `test_the_plan_lists_a_text_said_twice_once`): acceptable. I removed single-flight with the plan dedupe in place and ran the cross-batch test: green. By construction the reverse also holds, because without the plan dedupe the duplicate hashes in one batch are folded by single-flight. So the provider-once outcome now has two independent layers, and each layer is pinned by its own test: the plan dedupe by the plan-level test, single-flight by the unit test and its mutation. No outcome is left unguarded. What was lost: the workflow-level cross-batch test no longer discriminates either layer by itself, so the "two batches, same text" outcome is proven only while at least one layer is present; the combined removal is not mutated. That is a minor gap, not a blocker.

## M4/M5/M6/Lows: skim

- M4: `dubber_usage_limits(n) = 10n+20 / 10n+40`. The cost per line is a read, three rewrites, an accept (five tool calls) over a few turns; margin is about 2x. Test asserts growth and the `>= 60*5` floor; mutation caught. OK.
- M5: `text-vi` is now in `plan.md` (principle 5, AC2), phase 08 and phase 10 (diff checked). Also `plan.md:117` names `META_TEXT`. OK.
- M6: moved to "open questions for phase 08" in the phase file, with the flag read from `LocalizationResult.script`, which M1 now makes correct. Refused rewrites are logged as a warning (`dub_script.py`). OK; still only a log line.
- DUBBER_MODEL fallback (`providers/llm.py:14-20`): not a silent wrong-model risk. The fallback is the Localizer's model, which is already required, validated and prefix-handled, so a typo cannot select an unknown model. An empty string and unset behave the same (tested; mutation caught). The only residual is cost or quality (the same model for both roles) and the error text: if both are empty the message says `DUBBER_MODEL is not set` although the fix is either variable. L-d.

## New Low findings

- **L-a. A line cut by the trim is now silent when it was not marked.** `assemble_audio.py:69-75`. The only flag is `overflow_unresolved`, set from the slot. If a segment's slot itself runs past the video end (transcript `end_ms` greater than the video length) or its start is beyond it, the clip is cut or dropped and the run succeeds with no flag; before the change the length check failed loudly. Scenario: ASR timestamps rounded past the end by a few hundred ms drop a closing word. Fix: clamp the slot to the video duration when measuring fit (so the flag is set), or log a warning from `_build` when `start + duration/stretch > duration_ms`. No test pins this.
- **L-b. The ingest result is discarded on a dubbing-time ingest regeneration** (`video_localization.py:325-326`, `await self._ingest(spec)` with no rebind, `prep.ingest` stays the old object). Harmless while the ingest keys and `duration_ms` are deterministic for a given input; stale if ffmpeg output length ever differs. Fix: rebind `prep = prep.model_copy(update={"ingest": await self._ingest(spec)})`.
- **L-c. The detached request after a cancelled activity has no deadline of its own** (`synthesize.py:187-189`). It relies on the provider client's timeout. Confirm that timeout is bounded (not verified here); otherwise a hung request stays in `_in_flight` and every later caller for that text joins a task that never finishes. Fix: wrap `_clip` in `asyncio.timeout` equal to the activity timeout, or make sure the HTTP client has a total timeout.
- **L-d. The empty-model error names only `DUBBER_MODEL`** (`llm.py:19-20`). Say that either `DUBBER_MODEL` or `LOCALIZER_MODEL` must be set, and log once at startup which model the Dubber uses.
- **L-e. The call to `check_length` in `_build` is not mutated** (`assemble_audio.py`, the line after `render(...)`). The mutation deletes the comparison inside `check_length`, which the direct unit test catches; removing the call would leave the activity tests green because no activity test now produces a wrong length. Add a mutation or an activity test that mocks `render` to write a wrong-length file.

## Answers to the specific questions

- M1: fixed; replay safe; other return paths right (see above).
- M2: ffmpeg run on a synthetic overrun; length equals video length; chain bit-identical before the cut; no click at the cut; true-peak assertion holds; `overflow_unresolved` still set; the check is a tripwire, not dead (L-a, L-e).
- M3: no deadlock, no leak, cancelled caller semantics correct, failure not remembered, AC1 and hash intact; the harness narrowing does not leave the outcome unguarded.
- M4/M5/M6 and the model fallback: fine, see L-d.

Status: DONE
Summary: PASS. M1, M2 and M3 are fixed and verified (ffmpeg synthetic run: 10.000 s output, bit-identical before the cut, peak held; single-flight has no leak or deadlock), 702 tests pass, and the phase-06 harness catches all 61 mutations with 3 controls green. Five Low findings were found, none blocking.
Concerns/Blockers: The skill `backend-ai-toolkit:code-review` was invoked. Not run: the full `run-mutations.sh`, ffmpeg 5.1 of the worker image, and anything live. L-c depends on the provider client's timeout, which I did not read.
