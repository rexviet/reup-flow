# Source-grounded translation and review styling

The translator previously received damaged ASR and subtitle visibility times, but
no original subtitle words. This let it invent a fluent interpretation of corrupt
tokens. The repair adds independent, timestamped OCR evidence from the captured
generation source to both the initial Localizer and its repair request. No manually
written Vietnamese replacement is supplied to the model.

- Source frames are sampled throughout each segment, including near its boundaries.
  The detected subtitle band is padded and read at 1024px width. OCR copies source
  characters; Localizer compares them with ASR, adjacent speech and glossary.
- Provider output is closed-schema validated against the exact requested timestamps.
  Malformed readings retry through Temporal at most three times; completed segment
  readings are immutable and cached by generation, source, model, prompt and sample
  positions. Inputs from a different generation are rejected.
- Temporal marker preserves prior histories. Images stay outside workflow payloads;
  source text and combined context are checked against payload limits. Legacy delivery
  skips this step. Model configuration is unchanged.
- Latest styling: fixed 20px text, white box alpha 0.25, always visible, baseline
  centered width 60%. The box expands only during long captions; contiguous spans
  keep it present through gaps. Glyph outline is `-webkit-text-stroke: 2px #000`; Studio's Style Stroke
  controls the element border. Source text/timing/audio are unchanged by styling.

Manual caption edits were stopped and undone through CAS at the user's correction;
generation-2 revision `42b661479d36439bbedf9db644398104` restored the original captions
and mixed audio while retaining the then-requested layout. Subsequent native edits
in the generation-2 cache are preserved, including source edits outside the importer.

Generation 3 was started with the real pipeline after a database backup at
`backups/translator-canary-20261110-1948.dump` plus checksum. The first dispatch hit
a voice-catalog timeout; a fresh read confirmed the voice and the same unused
generation was dispatched successfully. Original run `01a1c58f-05ab-7e7f-94b2-2185f17aa579`
failed on malformed OCR output. Real repeated reads proved that output recoverable;
the classification was corrected without relaxing validation. Reset checkpoint 66
resumed at evidence collection as run `568f342e-a8c2-4ad7-a435-2f5e2fb51c8f`, preserving
completed prep. Use the SDK for this operation: the Compose `temporal-admin-tools`
service has a schema-init entrypoint, not a CLI entrypoint; the attempted help command
ran a no-op schema check (zero updates).

The actual AI-generated script has 36 segments. It independently corrected “pen” to
“fan,” “couldn't find a seat” to “couldn't stand steadily,” and the corrupt 24-second
phrase to “Người ta chia người thành đủ hạng…”. Original source evidence includes
`人分三六九等 / 肉分五花三层`. The revised script has 3 inferred and 3 unresolved lines;
uncertain material remains a human-review concern rather than invented speech.

Validation: latest focused regression 95 passed; source/provider/prompt/workflow and
permission recheck 77 passed before the last geometry-only test group. Earlier Linux
regression had 623 passed and 2 failures caused solely by omitted Compose/policy bind
mounts; those permission tests passed locally. Mac worker wiring checks require raqm
and failed that prerequisite; Linux wiring/replays passed. Scoped and full Python
Ruff checks and Git whitespace check passed. No failing prerequisite was disabled.

Generation 3 completed as `needs_human`; DB and Temporal agree, and history has zero
render calls. Initial HEAD: `8618924658be4413a5f24d8d94d6114f`. Final per-caption box
geometry was committed as `bda706e5c8cc461a80346121eafdea57`, with assertions that
cues and mixed audio did not change. Native Chrome verified a full 03:09 timeline,
corrected caption at 00:25, computed font 20px/line-height 25px, and playback from
00:25 to 00:40. Studio is left paused at 00:25, PID 70470 on loopback 3002. Companion
is connected to generation 3. Cold refresh while Studio was open passed; all 36
caption/audio pairs have zero mismatches.

Generation-2 native edits remain untouched, with an additional backup at
`backups/native-edits-g2-20261110-2003/g2`; the pre-style generation-3 cache is at
`backups/review-g3-layout-20261110-2003/g3`.

Repeated TTS failures were the HTTP client's 120-second deadline, not heartbeat
loss. Live VoiceStudio budgets reported execution 600s, queue wait 1800s and model
load 1200s; engine health stayed good and clips continued completing. The separate
`TTS_TIMEOUT_SECONDS` now defaults to 1200, bounded to 1500 below the synthesis
activity's 1800-second budget. STT/OCR keep 120s; HTTP error classification and cache
reuse are unchanged. Active worker settings were verified after deploy. Generation
3 finished around this rollout; this does not prove a new real request exceeding
120 seconds under the longer deadline. A real HTTP delay test verifies independent
TTS/STT timeouts and no hidden client retry. No native VoiceStudio process was killed.

Additional checks: Linux font/provider/worker regression 176 passed, including the
raqm-dependent cases; final geometry/bridge/source/provider checks 74 passed. Full
Python Ruff and whitespace checks passed. Starter remains `legacy_render`; no broad
activation, final render or publication ran.

Remaining limits: 3 inferred and 3 unresolved lines require review. Native style
persistence and broader rollout acceptance remain separate; this run did not click
the companion revoice button.
