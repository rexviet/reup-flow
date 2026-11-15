# Scout: Localizer blank lines, audio contracts, and Temporal replay

**Conducted:** 2026-11-08 (Asia/Ho_Chi_Minh)
**Scope:** read-only scout for the accepted design in
`plans/brainstorm/261108-2221-hyperframes-review-contextual-localizer.md`.

## Outcome

The pipeline currently has three hard blockers for “unrecoverable translation remains
blank, keeps its ID/timing, produces no TTS/SRT cue, and remains reviewable”:

1. Single Localizer output and repair classify blank text as `unusable`, then Temporal
   fails the run before `assemble_script` (`localizer_single.py:223-273`,
   `agents/temporal.py:339-359`). Tool mode also rejects blank output in its validator
   (`localizer.py:322-356`).
2. `assemble_script` rejects blank text as non-retryable `IncompleteScript`
   (`activities/assemble_script.py:37-87`). `LocalizedSegment` has no review reason or
   inference metadata (`contracts/models.py:147-169`).
3. The audio contract assumes one non-null TTS clip per script segment:
   `DubSegment.tts_key` is required and `DubSegmentsDocument` documents one entry per
   segment (`contracts/models.py:214-230`); synthesis, fit measurement, commit, and
   assembly all call/cache every segment (`activities/synthesize.py:259-266`,
   `activities/dub_script.py:90-107`, `163-198`).

The safest design is to keep every transcript ID and timing in the localized script and
dub-segments document, add an explicit “not spoken” representation for a blank line, and
make every TTS/layout/SRT consumer skip that representation. Do not silently drop IDs from
the durable documents: reviewers and a later manual fill need them. The audio assembler
already has a no-clip branch that emits only BGM, but a fully silent BGM still raises
`SilentAudio` (`activities/assemble_audio.py:47-77`, `134-155`), so the all-empty case
needs an intentional silence-track policy/test.

## Contents

- [Current Localizer behavior](#current-localizer-behavior)
- [Contract and propagation map](#contract-and-propagation-map)
- [Audio/TTS regeneration and stale-text protection](#audiotts-regeneration-and-stale-text-protection)
- [Temporal compatibility](#temporal-compatibility)
- [Recommended implementation boundaries](#recommended-implementation-boundaries)
- [Focused test matrix](#focused-test-matrix)
- [Workspace and verification notes](#workspace-and-verification-notes)
- [Unresolved questions](#unresolved-questions)

## Current Localizer behavior

### Contextual single mode

- `LocalizerDeps.expected_ids()` defines the requested ID range, including a zero-length
  range for an empty transcript (`agents/localizer.py:92-110`).
- `localizer_single.load_context()` reads the whole selected transcript, slots, detected
  language hints, burned-in subtitle overlap, and glossary terms once
  (`agents/localizer_single.py:101-139`). This is the correct place to preserve full
  transcript context for both initial translation and repair.
- The initial prompt says “no empty text” and requires one line per ID
  (`agents/localizer_single.py:52-72`). Repair receives only problematic lines, but it
  includes each line’s source, slot, current answer, and problem (`276-301`). Therefore a
  blank-recovery repair can retain context if the policy changes from fatal to flagged.
- `assess()` distinguishes missing/blank/unshowable (`Problem(kind="unusable")`) from
  overlong (`too_long`) (`223-273`). `merge()` only replaces asked IDs and leaves all
  other IDs untouched (`304-320`); this is reusable for a repair that keeps a blank when
  the model cannot justify a recovery.
- `lines_of()` emits only `(id, text_vi)` and discards `Problem` metadata
  (`382-385`). No inference reason, confidence, or “needs review” state can currently
  reach the persisted script.
- Durable one-request mode fails on any `assessment.unusable()` before the assemble
  activity (`agents/temporal.py:339-359`). This includes a line still blank after repair.
- Tool mode’s output validator rejects empty lines and retries the model
  (`agents/localizer.py:322-356`). Existing replay behavior must remain unchanged for
  histories recorded before a new blank-tolerant mode.

### Repair/merge implications

The accepted design asks for contextual repair even when only selected IDs are reviewed.
Current `run_localizer(..., feedback)` passes reviewer notes as untrusted prompt data, but
the one-request repair prompt only carries the failed lines plus glossary, not adjacent
transcript lines (`agents/temporal.py:295-359`, `localizer_single.py:276-301`). The initial
context object still contains the full transcript, so the repair prompt should add a bounded
neighbor/context block rather than reload or mutate transcript data. Keep source text and
review notes in untrusted blocks; never put inference reasons into spoken text.

## Contract and propagation map

| Stage | Current owner/evidence | Current blank behavior | Required design check |
|---|---|---|---|
| Localizer output | `localizer_single.py:203-273`; `localizer.py:322-356` | Missing/blank/unshowable is fatal/retried | Permit blank only as an explicit unresolved state; preserve duplicate/unknown-ID checks. |
| Merge | `localizer_single.py:304-320` | Asked repair replaces only requested IDs | Preserve blank + stable ID if repair omits/returns blank; preserve unaffected lines. |
| Assemble script | `assemble_script.py:37-87` | Exact ID set required; blank raises `IncompleteScript` at 65-67 | Accept blank; store timing/source/on-screen and review metadata. Keep exact IDs and duplicate guard. |
| Persisted script | `contracts/models.py:147-169` | `LocalizedSegment` has only text/timing/source/on_screen | Add backward-compatible optional/default status/reason fields; do not add required fields to old payloads. |
| Dubber single | `dubber_single.py:145-212` | Empty rewrite is unusable; decision fallback keeps prior text at max stretch | Blank initial lines must not enter pending fit/TTS IDs; nonblank rewrite remains current behavior. |
| Dub script | `dub_script.py:46-82` | Empty decision ignored; every script segment then synthesized in `90-123`, committed in `163-198` | Keep blank segment ID in document with explicit omitted/not-spoken state; skip `_duration`, cache lookup, and TTS. |
| Synthesis | `synthesize.py:259-266`, `292-327` | Hashes every segment text; one hash invokes provider/cache | Filter unresolved blank lines; retain provider/model/engine/voice/text/method hash for nonblank lines. |
| Track assembly | `assemble_audio.py:47-77`, `201-236` | `[]` placements means BGM-only track; silent mix fails at 134-136 | Skip omitted clip entries; keep original vocals out; define whether all-empty means BGM-only or generated silence, and test both. |
| Caption layout | `layout_captions.py:261-282`, `324-390` | Empty text is already skipped; no caption means no cover (`346-375`) | Keep no cue/SRT entry for blank while preserving script ID/timing in project review data. |
| Review | `review_checks.py:89-126`; `review_render.py:357-388` | Empty persisted line would be flagged as fidelity issue, but current assemble prevents it | Review must surface unresolved IDs/reasons without routing blank lines into TTS; ensure timing checks tolerate omitted audio entries. |
| Publish flags | `assemble_script.py:68-70`; `video_localization.py:742-755` | Only contact safety flags and refused Dubber rewrites propagate | Add unresolved/inferred IDs/reasons to durable review/publish reasons; do not mark localized output published solely because checks pass. |

There is no SRT writer in `services/localization/src/localization`; captions are currently
held in `EditSpec.captions` and rendered by `caption_images.py`. Any SRT/export step added
for HyperFrames must derive cues from nonblank caption spans only and retain the segment ID
in project metadata, not emit a blank SRT cue. `caption_spans()` already normalizes and
skips empty text (`layout_captions.py:269-282`).

## Audio/TTS regeneration and stale-text protection

The current per-line regeneration seam is reusable:

- `Synthesizer` is shared by batch synthesis and Dubber tools, so the same text is a cache
  hit (`activities/synthesize.py:153-176`).
- `tts_hash()` includes `CLIP_METHOD`, provider, model, engine, voice ID/settings, and the
  exact text (`73-87`). `CLIP_METHOD="pieces-1"` deliberately prevents old whole-line
  clips from satisfying piecewise synthesis (`48-52`). This is the correct versioning
  pattern for regenerated text: a changed line must hash differently; a provider/engine/
  voice change must also hash differently.
- Metadata records base64 text, voice hash, and measured duration (`36-44`, `215-244`),
  allowing tests/diagnostics to prove the clip says the script text. Keep that invariant
  for a one-line “create voice” action.
- In-process deduplication prevents concurrent duplicate provider calls for one hash
  (`193-208`), while cross-worker races may still call twice but write identical keyed
  objects (`193-198`).
- `segment_hashes()` deduplicates repeated nonblank lines in order of first use
  (`259-266`). Filter blanks there (or before it), not by inventing a provider call.
- After a Dubber rewrite, workflow synthesizes the final script again; unchanged lines are
  cache hits (`video_localization.py:642-668`). `commit_dub()` reads each final clip’s real
  duration and creates `DubSegmentsDocument` (`dub_script.py:163-198`). A manual edit action
  should use the same `Synthesizer.clip()` and then replace only the selected segment’s
  `tts_key`/duration while preserving all other clips and timings.
- `layout_captions.check_one_translation()` rejects mismatched script keys/versions and
  verifies each spoken text equals the script (`layout_captions.py:393-409`). Any per-line
  regeneration must create a new script/audio or a clearly versioned edit artifact, and
  must not mutate an already-published round in place.

For a blank line, do not call `voice_rate`/TTS as a proxy and do not use the original vocal
track. `assemble_audio` downloads only `ingest.bgm_key` and listed clips
(`assemble_audio.py:209-221`); omitting the clip leaves that interval without dubbed voice.
If BGM is present, it remains. If the product requires absolute silence for an unresolved
slot, the graph needs an explicit silence/ducking policy; the current no-clips graph is
BGM-only, not silence-only.

## Temporal compatibility

`VideoLocalizationWorkflow` explicitly requires `workflow.patched()` around command-order
changes (`workflows/video_localization.py:1-7`). Existing markers cover localize, dub, review,
review-before-render, STT rate, even TTS batching, ad and voice checks
(`73-100`). `agents/temporal.py` separately gates Localizer single mode, Dubber single/code
mode, and glossary-term checking (`71-84`, `312-359`).

Plan a new marker for the blank-tolerant contract if it changes the activity inputs,
activities scheduled, or result interpretation. Old histories must follow their old branch;
new histories record the marker before using optional blank metadata. Do not change the
meaning of existing markers. Replay fixtures assert markers and scheduled activity order,
including old histories that lack newer markers (`tests/replay/test_replay.py:143-190`,
`664-718`).

Backward-compatibility rules:

- New fields in `LocalizedSegment`, `DubSegment`, `DubSegmentsDocument`, or result models
  must be optional/defaulted so old JSON and Temporal payloads decode. `DubSegment.tts_key`
  is currently required (`contracts/models.py:214-220`); changing it to nullable is the
  smallest compatible representation for an omitted clip, with defaults for any new
  `spoken`/`skip_reason` fields.
- Keep old `script.vN.json` and `dub-segments.json` readable. `script_document.producing_stage`
  determines producer from version (`activities/script_document.py:9-35`); do not repurpose
  version semantics. New manually edited/regenerated scripts should increment the script
  version and use a new round/key, as `write_script()` already does (`38-41`).
- Existing runs created before blank support should not be retroactively reinterpreted as
  successful unresolved lines. New behavior should be behind the marker; old replays retain
  strict validation and their recorded activity schedule.
- Preserve Vbee as a separate workflow. It already handles an empty transcript as no slots,
  no shortening/TTS, and a music-only track (`workflows/vbee_dubbing.py:173-181`). Do not
  broaden the Localizer blank contract into Vbee’s slot pipeline; keep the shared
  `LocalizedScript`/audio compatibility defaults.

## Recommended implementation boundaries

1. **Contracts:** represent unresolved localization separately from text content (for
   example a defaulted status/reason field on `LocalizedSegment`), and represent omitted
   speech on `DubSegment` with nullable `tts_key`, zero duration, and a defaulted explicit
   state. Keep IDs, starts, ends, slot, source, and `on_screen` unchanged.
2. **Localizer:** make blank a flagged assessment, not a model output validation success
   and not an exception. Let repair use full context plus adjacent lines; merge only asked
   IDs. Emit a separate review metadata object/fields; never append reasons to SRT or TTS
   text. Keep missing/duplicate/unknown IDs fatal.
3. **Assembly and audio:** accept blank in `assemble_script`; filter unresolved lines from
   synthesis plans, fit reports, Dubber pending IDs, and `commit_dub`; write explicit omitted
   entries; make `assemble_audio` skip them. Ensure all-empty output has an intentional
   non-original-vocals result and no `SilentAudio` surprise if the accepted product policy
   is silence.
4. **Layout/export/review:** skip empty captions (already done), emit no blank SRT cue, keep
   unresolved IDs/reasons in HyperFrames project review data, and teach timing/review checks
   that omitted speech is intentional rather than a missing clip.
5. **Manual per-line regeneration:** route selected ID + current script version + edited
   text through the existing `Synthesizer`; key by exact text and voice identity; measure
   real duration; reject/flag overflow without shifting later starts; write a new versioned
   artifact and replace only that line’s clip.

## Focused test matrix

Run from `services/localization` (the `pyproject.toml` config sets `pythonpath = ["src"]`,
asyncio auto mode, and excludes `live` by default):

```bash
uv run pytest -q tests/agents/test_localizer.py tests/agents/test_one_request_agents.py \
  tests/agents/test_assemble_script.py
uv run pytest -q tests/activities/test_synthesize.py tests/activities/test_dub_script.py \
  tests/activities/test_assemble_audio.py
uv run pytest -q tests/activities/test_layout_captions.py tests/activities/test_review_render.py \
  tests/workflows/test_one_request_agents.py tests/workflows/test_review_loop.py
uv run pytest -q tests/replay/test_replay.py
```

Add/retain these cases in the implementation plan:

| Case | Assertions |
|---|---|
| Initial Localizer cannot recover ID 3 | Full contextual prompt/repair; ID 3 remains in script with timing; reason is metadata; no model-loop failure. |
| Repair returns blank/omits requested ID | Merge preserves prior unresolved state; unaffected IDs/text unchanged. |
| Missing/duplicate/unknown IDs | Still fatal/retried; no partial script written. |
| Mixed inferred + blank lines | Inferred text contains no reason marker; inferred/blank flags reach review/publish metadata. |
| One blank among spoken lines | Synthesis plan excludes blank; TTS provider receives only spoken texts; `DubSegmentsDocument` retains blank ID with explicit omitted state. |
| All lines blank | Zero TTS calls and zero captions/SRT cues; stable IDs/timing remain in project/review data; audio follows explicit BGM-only vs silence policy; no original vocals. |
| Manual fill of one blank | Only selected ID gets a new TTS clip/hash and measured duration; other clip keys, starts, and timing remain unchanged; overflow is reported, not propagated to later lines. |
| Stale text/cache | Changing text, engine, voice settings, provider, or `CLIP_METHOD` changes hash; old clip metadata cannot satisfy new text. |
| Version mismatch | Layout/assemble reject script/audio/doc from different versions; new round writes new keys and leaves old round untouched. |
| Existing Temporal histories | Old phase-04/05/06/08 histories replay unchanged without new marker/activity; new histories record blank-support marker and replay deterministically. |
| Vbee empty transcript | Existing Vbee behavior remains no slots/no TTS/music-only; shared contract defaults decode old Vbee scripts. |

### Verification run in this workspace

I ran:

```bash
cd services/localization
uv run pytest -q tests/agents/test_one_request_agents.py \
  tests/agents/test_assemble_script.py tests/activities/test_synthesize.py \
  tests/activities/test_assemble_audio.py tests/activities/test_layout_captions.py
```

Result: **131 passed, 35 failed**. All 35 failures were `LayoutCaptionsActivity` construction
failures because this environment’s Pillow reports `features.check("raqm") == False`
(`layout_captions.py:426-429`), not blank-line assertions. The non-layout tests in that
command passed. Install/use the project image/runtime with libraqm before treating layout
tests as a product regression.

## Workspace and verification notes

The worktree was already dirty before this report. Preserve all user changes; I did not
modify or inspect secret contents. `git status --short` showed modified:

`README.md`, `docker-compose.yml`, `scripts/bootstrap.sh`, `scripts/e2e-smoke.sh`,
`services/extension/README.md`, `services/extension/options.js`,
`services/localization/Dockerfile`,
`services/localization/src/localization/activities/localizer_ports.py`,
`services/localization/src/localization/activities/transcribe.py`,
`services/localization/src/localization/agents/localizer.py`,
`services/localization/src/localization/agents/localizer_single.py`,
`services/localization/src/localization/contracts/models.py`,
`services/localization/src/localization/providers/openai_stt.py`,
`services/localization/src/localization/providers/registry.py`,
`services/localization/src/localization/workers/run.py`,
`services/localization/tests/agents/test_one_request_agents.py`, and
`services/localization/tests/workers/test_agent_wiring.py`.

Untracked paths included `.env.bak-261108-180934` (not opened), the accepted brainstorm,
multilingual spikes, extension test, speech chunk activity/test, and multilingual STT test.
Do not overwrite or clean these while implementing.

## Unresolved questions

1. For an unresolved line with background music, is the accepted output BGM-only in that
   interval, or must the interval be absolute silence? The current graph supports BGM-only;
   the requirement “keep silence instead of original vocals” needs this clarified.
2. Which exact project/SRT artifact should carry per-line inference reasons and unresolved
   status? The current repo has no SRT writer; `EditSpec.captions` is the only caption
   contract found.
3. Does the manual “create voice for this line” action need a new workflow/activity API,
   or will it be an external HyperFrames integration calling an existing provider seam?
   Either way, it must use the same text/voice/method hash and script-version checks.
