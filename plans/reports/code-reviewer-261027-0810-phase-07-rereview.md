# Phase 07 re-review: fixes in 03e5b19

Branch `feat/localization-phase-00-infra`, range `a04f571..HEAD` (03e5b19). Skill `backend-ai-toolkit:code-review` was invoked (spec compliance of each fix, quality, adversarial). Scope: the M1-M7 fixes and regressions they could cause. Review only, nothing modified or committed.

## Verdict: PASS (no Critical, no High). Two Medium and five Low new findings. The phase correctly stays `in-progress`.

All seven earlier Medium findings are fixed in substance. The two Medium findings below are gaps inside those fixes: the single-flight (M4) lets one caller's cancellation fail another caller's activity, and the new ceilings (M1) are larger than the budget that was measured (M5).

## What was run

| Check | Result |
|---|---|
| `uv run pytest -q tests/activities/test_layout_captions.py tests/integration/test_font_parity.py tests/test_edit_spec_schema_drift.py tests/test_object_store.py -rs` | 61 passed, 0 skipped (1 deselected by `-m 'not live'`). Font parity run alone: 3 passed. |
| `uv run pytest -q tests/test_dockerfile.py tests/test_worker_settings.py tests/test_contract_hardening.py` | 83 passed |
| `uv run ruff check src tests` | clean |
| renderer `pnpm test` | 82 passed (5 suites) |
| renderer `pnpm lint` | clean |
| renderer `tsc --noEmit` | clean |
| Own probes (scratchpad only) | ffprobe spoof table, glyph coverage table, single-flight cancel probe (below) |

Not run, so not claimed: any mutation harness (`run-mutations.sh`, `tests/mutations/*.sh`), by the user's rule. The mutation entries were judged by reading only. `pnpm test:integration` was not run (needs the MinIO proxy; no new long-lived service started). `downloader:infra`, the full compose stack, a real render, and anything against real AWS S3 or R2 were not run. Nothing of mine is left running, and the Temporal dev server of another session was left alone.

## Findings (new)

### Medium

**N1. The shared render belongs to its first caller: that caller's cancellation fails the waiter, and the waiter has no heartbeat, deadline or cancellation of its own.**
`services/renderer/src/activities/render.ts`, in the returned `render()`: `const running = inFlight.get(sha); if (running) return running;`. `renderOnce` builds its `signal` and its heartbeat timer from the first caller's `Context.current()` and cancellation signal.
Probe, with the real `createRenderActivity` (tmp file in the scratchpad): caller A starts the render, caller B asks for the same spec, A is cancelled. Result: B rejects with `Error: aborted-by-first-caller`, and `renderMedia` was called once. The timer that heartbeats during B's wait is A's.
Consequences:
- B's activity fails with A's cancellation. It is retried because the error is generic, so it recovers, but only after the render was killed for no reason of B's. This is exactly the "a cancelled caller must not cancel shared work" case, and no test covers it (the cancel test and the single-flight test are separate).
- B's own cancellation is ignored: a cancelled B keeps waiting until the render ends.
- B never calls `heartbeat` from its own context. If the workflow sets a heartbeat timeout (phase 08 will), B times out while waiting, and cancellation is delivered to an activity through heartbeats.
- A retry attempt that starts while the previous attempt's render is still being killed joins that dying render and fails with its abort error.
Reachable only with `RENDER_CONCURRENCY` above 1, or an overlapping retry. The default of 1 hides it.
Fix: give the shared render its own `AbortController` driven by the deadline and a count of live callers (abort it only when the last caller has gone). Each caller awaits it with `Promise.race` against its own cancellation signal, and heartbeats from its own context. Add a test where the first caller is cancelled while a second is waiting and the second still gets the result. Add a mutation for it in `phase-07.sh` (none of the 55 covers it).

**N2. The new ceilings admit inputs that the measured memory and `/tmp` budget does not cover.**
`contracts/models.py` (`MAX_FRAME_PX = 4096` per side, 15 min), `docker-compose.yml` worker-render. The only measurement is one 60 s 1080x1920 clip (2.07 MP): peak 4.1 GiB, 52 MB `/tmp`, about 40 MB shm, and a 4 GB container was OOM-killed. The ceiling is 4096x4096 (16.8 MP, 8 times the pixels) and the layout test `test_a_video_at_the_limits_is_accepted` accepts it. A plain 3840x2160 upload (8.3 MP) is accepted too.
Scenario: an in-limit 4K source is rendered; Remotion's frame buffers scale with pixels, the container hits 6 GB, and the OOM killer takes the whole worker process (all activities on it, not just this render). Temporal retries the activity into the same OOM. `/tmp` is the second constraint: 52 MB per 60 s extrapolates to about 780 MB at 15 min against the 1 GB tmpfs, before 4K output sizes.
The fix did bound the frame side and duration. It did not bound what the budget was sized for.
Fix: cap by area, for example `width * height <= 1920 * 1088` (portrait 1080x1920 passes), or measure a 4K clip and size `mem_limit` and tmpfs from that. Keep the model and the schema in step (`maximum` alone cannot express an area cap; add a model validator and a Python-side check, and a Node-side check in `createSpecValidator`).

### Low

**N3. The 30 min deadline and the 15 min cap contradict the measurement.** `render.ts` `DEFAULT_RENDER_DEADLINE_MS` and its comment ("the spec's own limits keep honest renders far under it"). The phase file records 182 s wall for 60 s of video (3x real time). A 15 min video would need about 45 min, so any video over about 10 min that passes layout burns 30 min of the only render slot and then fails for good (`RenderDeadline`, not retried). The product looks to be about 3 min (plan G6), so this is latent. Either lower `MAX_DURATION_MS` to about 9 min, or raise the deadline, or at least correct the comment and write the ratio in the phase file. Related edge: after the deadline fires, the `signal.throwIfAborted()` calls around download and upload raise a generic `TimeoutError` (retryable), not `RenderDeadline`; a render that finishes just past the deadline is discarded and redone.

**N4. 403 on HEAD means "missing": acceptable, but it has a cost and a better primitive.** `services/renderer/src/object-store.ts` `exists`, `localization/storage/object_store.py` `_is_missing_on_head`.
- Mask risk is low on the renderer side: the same activity has just read the spec with a GET on the same prefix, so the credentials work. A HEAD-only 403 on a `work/` key in practice means "not there". An overwrite is harmless: the key is the content hash.
- The cost: a real 403 that appears later (expired or rotated key, bucket policy change) is now found only at upload, after a full render of up to 30 min. A retry loop then costs a render per attempt. HEAD has no body, so the error code cannot separate the cases.
- On the Python side the change currently has no caller (`exists` and `metadata` are used only in tests; layout no longer calls `exists`). A future cache check that treats 403 as a miss will recompute and overwrite instead of failing loudly.
- Better fix, if the production store turns out to return 403: `ListObjectsV2` with `Prefix=<key>` and `MaxKeys=1`. It is allowed under the existing prefix-conditioned `ListBucket`, gives 200 with zero keys for "missing", and gives a real `AccessDenied` for a real refusal. Verify on AWS S3 or R2 before phase 10 (still unchecked, disclosed).

**N5. "Always copy the source" does not make the render fresh.** `layout_captions.py`, `phase-07` file ("nguồn tải lại không bị render cũ"). The copy is refreshed, but the render key is the spec hash, and the spec names `source_key` and carries no source hash. A re-downloaded source with different picture bytes but the same size, fps, length and the same audio (so the same `audio_sha256`) gives the same spec, and the renderer returns the old mp4. The new test asserts only that the copy happens. Impact is small (the audio hash usually changes when the source audio changes). Either put the source's ETag or sha256 in the spec (the right fix, and `spec_version` should come with it) or soften the claim in the phase file.

**N6. Phase 08 does not know about the new permanent failures.** `UncoveredGlyphs`, `ImplausibleCover`, `VideoOutOfBounds`, `InvalidEditSpec`, `FontMismatch` and `RenderDeadline` are named only in `phase-07-captions-and-renderer.md` (the M3 decision and its consequence are recorded there, with reasons: good). `phase-08-reviewer-and-publish.md` has no routing for them, so as written a stray CJK character would end the run. Judgement on "fail, not flag": acceptable for phase 07 as a stopgap, because burning a tofu box into a published video is worse than stopping. It is not acceptable as the final behaviour: the check runs after TTS and dubbing have been paid for, nothing earlier rejects such text (the Localizer has no charset validation, and `vi_syllables.py` even counts CJK tokens, so they are expected), and emoji, arrows (`→`, `✓`) and full-width punctuation (`，`, `！`) all fail it. Fix: add a phase 08 line routing `UncoveredGlyphs` to a Localizer retry with the list of characters (or `needs_human`), and `ImplausibleCover` to `detect_subtitles` or `needs_human`. Consider the same check inside the Localizer's validation so it fails before TTS.

**N7. Small ffprobe edges.** `activities/ffmpeg.py` `_fps`: when `avg_frame_rate` is `0/0` it falls back to `r_frame_rate`, which on some variable-rate files is a timebase such as `90000/1`; the file is then refused for good as over 120 fps although it is legitimate. `nan` or `inf` in `format.duration` raises an uncaught `ValueError` or `OverflowError` outside the `try` (retryable, never succeeds); ffprobe prints `N/A` instead, so this is theoretical. Pre-existing and not caused by 03e5b19, noted because the new caps depend on these values.

## Answers to the review questions

**M1 caps, both sides, spoofing.** Enforced in Pydantic (`EditSpec`), in the regenerated schema (`maximum` 4096, 120, 900000, `maxItems` 2000; drift test passes), in Node (Ajv `strict`; `edit-spec.contract.spec.ts` has over-limit and exactly-at-limit cases) and in `layout_captions.check_renderable`, which refuses permanently before any copy. A model `ValidationError` in `lay_out` becomes a non-retryable `InvalidEditSpec`. Probe table, run through the real `parse_probe` and `check_renderable`:
- portrait 1080x1920, 60 s, 30 fps: accepted.
- fps `1000000/1`: refused; avg `0/0` with r `1000000/1`: refused; fps field missing: defaults to 30, accepted.
- format duration `1e7`: refused; format missing with stream 99999 s: refused; format 10 s with stream 99999 s: accepted as 10 s (the render is only 10 s long, so no resource risk); `N/A`: permanent probe error; `0`: refused ("no length").
- 7680x4320: refused; 4096x4096: accepted (see N2).
- rotation side data: the coded size is used, so rotation cannot push a dimension past the cap (it can only swap two values that are both capped). Wrong box orientation for rotated sources is the old L9.
Cancellation reaches Remotion via `makeCancelSignal`; deadline and activity cancellation are merged with `AbortSignal.any` (Node 22 image). `selectComposition` is not cancellable, which is negligible.

**M2 font hash.** `font_sha256` is a required, pattern-checked field of the closed schema, so it is part of the canonical JSON that both `spec_sha256` (Python) and `specSha256` (TS) hash, and therefore part of the render key. Hashing parity holds by construction: both sides hash the raw bytes of the file and the canonical JSON function is generic over fields. `checkFont` runs in the activity and in `cli.ts`; the font served to Chromium is a copy of the checked file. Older specs lacking the field are refused as `InvalidEditSpec`; none exist outside fixtures (nothing in this phase has produced one), and the lack of `spec_version` is already a known Low.

**M3 glyph coverage.** Probe with `BeVietnamPro-SemiBold.ttf`:
- Vietnamese in NFC and in NFD, digits, ASCII and curly punctuation, `₫ € ™ ® ° × ½ • …`: no false positives.
- Space-like and invisible characters (tab, newline, NBSP, ZWSP, ZWJ, U+3000, BOM) are skipped by category (`Cc Cf Zs Zl Zp`); no false positives. U+3000 is skipped without checking the glyph, which could draw a box in a font-less Chromium; negligible.
- Flagged: `你好`, `😀`, `❤` plus its variation selector U+FE0F (category `Mn`, not skipped, but it only occurs with an emoji that is flagged anyway), `→`, `✓`, `，`, `！`.
- A combining mark on a base without a precomposed form (`x` + U+0302) passes because the font has the mark.
The decision (fail, not flag) and its consequence are recorded in the phase file; see N6 for what is missing.

**M4 single-flight, cancellation, heartbeat, leak.** One `renderMedia` per spec hash in a process: tested with a control. The owner's `finally` always deletes the map entry, so there is no leak on error or on cancel (and a failed render can be rendered again: tested). The heartbeat timer runs across download, render and upload for the owner and is cleared in `finally`. Gaps: N1 (a waiter has no signal, heartbeat or deadline, and inherits the owner's cancellation). If Remotion ever ignored the cancel signal, the entry would stay and every later request for that spec would attach to a hung promise until the worker restarts; theoretical.

**M5 memory.** Arithmetic is consistent: 6144 MB is at least 4200 + 1024 + 512 = 5736 MB, and `compose-constraints.spec.ts` enforces it. Measurement is one clip on one machine (disclosed); see N2 for what it does not cover.

**M6 cover bound.** 35% of the height and 40% of the area, tested with a tall but plausible band accepted, and the thin-cover case gets a 120 px two-line box (`caption_box_for`, clamped to the frame). Rejecting rather than falling back to the safe zone is a decision that is not stated with its rationale; the consequence belongs in phase 08 (N6).

**M7 403 on HEAD.** See N4.

## Mutation entries (read only, none run)

Counts in `scripts/run-mutations.sh` match the files: `renderer:07` declares 55 mutations and 5 controls and `phase-07.sh` has 60 `mp_mutation` entries of which 5 are controls; `downloader:infra` declares 22 and 2 and has 22 `run_mutation` and 2 `run_control`. New entries name the exact tests that must fail and use the existing protocol. The control that removes the decomposition fallback is correct (the font has every Vietnamese letter whole). Missing coverage: N1 behaviours (waiter keeps its result when the first caller is cancelled), the `Cc/Cf/Zs` skip in `missing_chars`, `MAX_CAPTIONS` on the model, the area bound of N2 when it exists. The phase file honestly says mutation evidence for the final state is pending.

## Cleanup
Probe files are in the session scratchpad only. No process or container of mine remains; the working tree has no changes other than the four untracked report files that were there before, plus this one.

## Unresolved questions
- What is the product's maximum source duration and resolution? It decides whether N2 and N3 are real (plan G6 suggests about 3 min, 1080p).
- Which production object store (AWS S3 or R2)? It decides whether the 403 mapping (N4) is needed at all.
