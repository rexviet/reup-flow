# Phase 04: Companion editing and one-cue revoice

The companion, durable editing/revoice backend and guarded host launcher are implemented.
Phase 04 is **not fully accepted**: native Studio Host/Origin and close/reopen/override
compatibility gates remain open, and the dedicated real-provider/backend smoke was skipped.
Review delivery remains disabled; Phase 05 rollout and production migration were not run.

## Delivered

- A Vietnamese companion edits saved caption text/timing, exports literal SRT and shows
  active spoken text/mismatch separately. Blank captions retain their review row and old
  active audio. Saving does not invoke TTS or move following clips.
- Immutable manifests/assets and HEAD ETag CAS preserve concurrent work. Stable cue-ID
  visual properties survive word-count changes. Unsupported source edits return conflict
  and stay on disk. Studio HTML/CSS/JSON sidecars are generation-scoped immutable assets;
  native override behavior still requires the open compatibility gate.
- Local projection locks, fingerprints and an interrupted-write journal allow clean
  cold-open/recovery while refusing unimported or concurrently changed Studio files.
  Additive manifest ancestry identifies an already-committed request after a lost result
  write, including when a later save has advanced HEAD.
- Standalone `RevoiceCaptionWorkflow` carries only refs and runs TTS/media/commit on the
  existing queues. It reuses `Synthesizer`, `TtsByProvider` and `ConfiguredVoices`; tests
  cover VoiceStudio and Vbee routing without changing the legacy Vbee output path.
- Only the selected clip is synthesized. Real ffmpeg applies atempo up to 1.15, rebuilds
  one mixed track with existing clips/BGM, and checks its length. Overflow, failure and
  stale results retain the caption/old audio; conflicting candidates remain inspectable.
- Immutable request/clip/result refs and deterministic workflow IDs recover retries,
  AlreadyStarted and lost HTTP responses. A CAS-protected queue caps unfinished
  reservations at 16 across bridge restarts, independently of Temporal visibility lag.
- Exact Host/Origin/CSRF, 60-second one-use grants, rotated eight-hour HttpOnly/Strict
  sessions, no-store/referrer headers, CSP and literal DOM rendering protect the bridge.
  Compose passes only review identity/settings, publishes loopback 3310, and has no socket
  or provider/DB credentials. MinIO policy changes were verified as templates, not applied.
- The foreground launcher validates identifiers/paths, checks the exact Studio pin,
  tracks PID/command/project and refuses collisions/reused PIDs. It probes an empty
  Studio project before materializing real data and fails closed on an unsafe server.
  Stale launcher locks are recoverable without stopping another process.

## Verification

| Check | Result |
|---|---|
| Bridge/revoice, real ffmpeg, real local Temporal/replay, project/contracts | 51 passed |
| Host launcher unit tests | 5 passed |
| Existing project/contracts/replay/Vbee/generation/review workflows | 440 passed |
| Existing audio/dub/synthesis/production worker registration, Linux raqm image | 152 passed |
| Synthetic HTTP/Temporal browser interaction check | Passed |
| Render checker at 375, 768 and 1440 px | Zero errors/warnings |
| Ruff across src/tests, schema freshness, uv lock, JS syntax, Compose and shell syntax | Passed |
| Dedicated real-provider/backend/Studio gate (`-m live`) | 1 skipped; prerequisites absent |

Counts overlap and are not a distinct-test total. The browser fixture uses synthetic media,
Moto and a counted test TTS provider; it proves actual HTTP/Temporal/ffmpeg/browser behavior,
not production S3 permissions or a live provider. The browser check verifies grant-fragment
removal, revoice/reload recovery, mixed-track playback, literal caption/SRT and preservation
of an unsaved second row. Native launcher tests cover its contracts; they do not prove the
pending Studio lifecycle gate. The available Node 22.12 runtime can run these browser
checks; opening the pinned Studio requires Node 22.13 or newer.

Inline ak-code-review found and repaired preview scaling/playback, unimported source
protection, stale-cache visual selection, queue limits, stale launcher locks and a child
exit race. The required ak-cook simplifier made scoped backend simplifications and passed
its focused checks. No independent reviewer approval is claimed.

The first host worker checks hit the existing missing-raqm limitation; the unchanged
checks passed in the Linux image. Browser verification initially found a missing favicon
and clipped preview; both were fixed. A final browser harness reload race was fixed by
waiting for the status element. The skill render checker initially lacked global WebSocket
under the default Node runtime; it passed under the installed Node 22 runtime. pnpm was
absent and old Corepack failed signature verification; the installed formatter API was used
without bypassing signature verification or changing package-manager configuration.

A process inspection briefly exposed a short-lived grant for the synthetic fixture; its
epoch was immediately rotated. No production credentials were involved. Test processes
and their local Temporal/browser children are stopped at finalization. No application
service was restarted, database changed, credential provisioned, or commit created.

## Visual review

The rendered product is compact, calm and legible. Favicon, responsive preview fitting and
neutral synced-state styling corrected the weakest first-pass details. No overlay exists.

| Item | Score / 2 | Evidence |
|---|---:|---|
| Brief fidelity | 2 | Dark editing desk and stable cue rows match the brief. |
| Hierarchy | 2 | Preview, caption form and explicit save/revoice actions are distinct. |
| Spacing | 2 | Card padding and larger section gaps remain consistent. |
| Alignment | 2 | Forms align; mobile recomposes to one column. |
| Typography | 2 | One system family and tabular timing values. |
| Color | 1 | Amber ready notice is more prominent than necessary. |
| Depth/shape | 2 | Hairline borders and one radius system. |
| States | 2 | Loading/error/disabled feedback and text mismatch labels. |
| Responsiveness | 2 | No overflow or sub-44px controls in the three viewport reports. |
| Unnecessary decoration | 2 | No ornamental dashboard metrics or marketing sections. |
| Content | 2 | Specific Vietnamese actions and active spoken text. |
| Craft | 1 | Native scaled video controls remain small on narrow screens. |

Total: 22/24. Screenshots/reports are ignored artifacts under `data/phase04-ui/`.

Operational setup and recovery are documented in [review-companion.md](../../docs/review-companion.md).
Remaining gates: native Studio security/round-trip, dedicated live smoke, then Phase 05.
Local journal recorded; AgentWiki publish skipped.
Unresolved questions: None.
