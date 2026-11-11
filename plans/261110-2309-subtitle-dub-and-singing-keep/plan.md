# Dub on-screen subtitles with no speech; keep original sound for singing

Outcome: a burned-in source subtitle that no transcript segment covers (g1 of video
7691886043717039013: 17.9-21.9 s and 27.4-31.7 s) is read, translated, captioned and
dubbed like spoken dialogue. A span that is singing keeps the original sound: no TTS,
no dub over it, and the original vocals stay audible there.

Constraints: new defaults are 20px / 4px black stroke / 28px line gap. Existing
workflows in flight must not break replay: every workflow change sits behind
`workflow.patched`. No change to existing generations; g1 needs a new run.
Non-goals: fixing OCR errors by hand, re-timing speech, publishing.

Acceptance: a new run of 7691886043717039013 has cues for the two gaps above; singing
spans have no clip and the mixed audio carries the original vocals there; focused tests
and Ruff pass.

## Phases

- [x] 1. `complete_transcript` activity: spans of `SubtitleDocument.cues` not covered by any
  transcript segment are read with `VisionOcr.read_subtitles` (frames via
  `source_evidence.source_frame` on the subtitle band), merged into a new transcript
  document (same prefix, new key) as segments with `source = "subtitle"`. Workflow calls it
  after `_prepare` behind a new patch. Cache per span like source evidence.
- [x] 2. Singing detection: classify each transcript segment on the vocals stem as
  `speech` or `singing`; add `keep_original` to the script segment. Method: audio-capable
  model through OpenRouter on the segment's vocals clip (new `singing_model` setting), no
  guess when unset (everything stays speech and the run records that it was skipped).
- [x] 3. Dub: segments with `keep_original` skip planning/TTS (plan_slots, synthesize);
  `assemble_audio` mixes the vocals stem, unducked, over those spans.
- [x] 4. Render defaults: `layout_captions` fixed 20px, 4px stroke, 28px line gap, matching
  `review_project.py`.
- [ ] 5. Tests, Ruff, rebuild worker, new run, check g-new at 18 s and 29 s.
  Tests and Ruff done (2026-11-11); rebuild, new run and the check at 18 s / 29 s remain.

Risks: workflow replay (patch it); OCR text can be wrong (mark cues `inferred` so the
Studio flags them); singing detector accuracy depends on the model.

## Decisions confirmed by the user (2026-11-10)

- On-screen subtitle with no speech is dubbed like dialogue (not caption-only).
- Singing detector: `google/gemini-3.1-flash-lite` via OpenRouter, audio input (`input_audio`,
  wav). Benchmark on g1: 38/40 correct, ~$0.00006 per call; `openai/gpt-audio-mini` refused
  to analyse audio (4/40 valid answers) and is not used. Segments under about 2 s are
  never classified as singing (the two benchmark errors were 0.9 s and 1.4 s lines).
- Boundaries come from transcript segment times, not fixed windows.
- Mix for a singing span: original vocals + background, background NOT ducked, no TTS.
  Mix for a speech span: background ducked, vocals dropped, Vietnamese dub over it, as today.
  Short fades (about 60 ms) at singing/speech edges.
- Demo of the target mix: scratchpad `demo-final-mix.mp3` (approved by the user, except the
  cue-21 clip below).

## Added phase

- [x] 0. TTS clip guard: g1 cue 21 "Tiếc là người Trung Quốc." came back as a 287 ms clip
  (87 chars/s; median is about 20). `SynthesizerActivity._clip` stores whatever the provider
  returns and the cache keeps it. Reject a clip far shorter than the text allows, retry, and
  never cache a rejected clip.
