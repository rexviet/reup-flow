# Live VoiceStudio and review canary

The user's two provider prerequisites passed. The latest video was explicitly started
as a new review-project generation, while normal dispatch remains legacy. Generation 2 completed as `needs_human`; the durable bundle and backend revoice were
verified. Native Studio interaction remains blocked.

## Provider evidence

- The local VoiceStudio runtime matches the saved patched checksums. Version reported
  by health is 0.5.7; the live multipart STT schema exposes `multilingual`.
- Voice `ngoc-huyen`, ID `c4bbe9c2`, is still present. This is the existing voice the
  user referred to as `ngonc-huyen`, not a replacement voice.
- A six-second real speech clip transcribed with `faster-whisper`, `multilingual=true`:
  7.52 seconds, two Korean segments and nonempty text. This verifies inference after
  model loading rather than relying on health/model-list metadata.
- TTS using `omnivoice` and that same voice produced a valid 2.13-second WAV in
  27.34 seconds. Local artifacts are ignored under `data/live-review-261110-1523/`.

## Authorized canary and operational changes

- Latest downloaded video and most recent existing run both identify `7689288513854595771`.
  Generation 1 was `needs_human`; generation 2 uses workflow
  `video-7689288513854595771-g2`, recorded `delivery_mode=review_project` and voice `c4bbe9c2`.
  The old outbox generation and existing run were not changed.
- Before any live schema/run writes, `scripts/apply-migrations.sh` created
  `backups/migrations-20261110-152611-2597.dump`, recorded SHA-256 and verified a restore clone.
  Only the nullable project-pointer migration was newly applied; the old ledger was retained.
- Built the current localization images, then updated agents/media workers and starter.
  There were zero running workflows and zero pending outbox rows before the update.
  Starter remains `legacy_render`; only the explicitly requested canary uses review delivery.
- Previous running image IDs were retained under each service's
  `before-review-261110-1523` tag. These are forensic fallback artifacts, not permission
  to roll workers back while recorded project/revoice histories still require the new code.
- There was no existing `output/7689288513854595771.mp4` before the canary.

## Observations during execution

Ingest and subtitle detection completed. STT recovered from a first-attempt timeout.
Its saved transcript has 38 segments with chunk-detected language labels: 19 Korean,
10 Chinese and 9 English. Those are model guesses, not independently verified labels.
The first localized script has 32 translated and 6 inferred lines, with reasons for
inference and no blanks. TTS recovered from HTTP 429 using the existing retry path; later requests succeeded.
No timeout/concurrency setting was changed speculatively.

## Final result

- Workflow `video-7689288513854595771-g2` completed, and its DB row is `needs_human`.
  `project_key` is `review/7689288513854595771/g2/head.json`; both `output_key` and the
  legacy MP4 `review_key` are null. Generation 1 and outbox generation 1 remain unchanged.
- All HEAD/manifest dependencies materialized with valid checksums. There are 38 cues:
  32 translated, 6 inferred with reasons, and no blank captions. The manifest records
  voice `c4bbe9c2` and model `omnivoice`.
- The generated project HTML has yellow `#ffff00` captions, black 2px stroke, a
  translucent `rgba(0,0,0,.55)` source mask and 12px backdrop blur. These are checks of
  the real saved HTML; native Studio rendering/persistence was not accepted.
- History contains no `render` activity. `publish` only persisted review metadata in
  this delivery mode. No final output object was created.
- Reviewer requested human review for the six inferred cues, omission/fidelity checks,
  and an end-of-video timing issue. The result is not an automatic quality approval.
- Two backend revoice requests for cue 12 completed through the actual
  `RevoiceCaptionWorkflow`/TTS/media/commit queues. They reused the existing matching
  clip cache; captions and all other cues were unchanged, while HEAD advanced by CAS.
  Fresh provider TTS inference was separately verified by the warmup sample above.
  This does not claim that a native Studio/companion button was clicked.
- Final HEAD revision is `8a24cad9f6cc05a574be93d0fb4e3f12`. A fresh smoke check
  materialized this bundle successfully and exited 4, the documented human-correction
  result. There are zero running workflows; default starter delivery remains legacy.

## Human-review boundary

The native launcher was retested under Node 24 with an empty scratch project. The
pinned Studio still failed its exact Host/Origin gate, before real review data was
mounted. The bridge's dedicated identity is not configured; the review profile was
not enabled and no credentials were reused from a worker/root identity.

The user explicitly requested this single-generation canary after the earlier phase
report. This does not approve bypassing the Studio gate or changing the default mode.
Native Studio opening and its live button interaction must not be reported as passed.

Unresolved questions: None. Native Studio compatibility remains a technical blocker.

## Handoff

Updated localization services are intentionally left running as the application's
managed Compose services. The existing VoiceStudio app was not restarted, overwritten
or duplicated. One-shot canary, monitor and revoice processes finished. No Studio or
bridge is listening on ports 3002/3310. The README now reflects the applied additive
schema/current compatible workers; full rollout and bridge credential provisioning
remain gated. No commit or publication was created.
