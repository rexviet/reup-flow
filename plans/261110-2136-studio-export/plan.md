# Repair native Studio export

Outcome: the current review project passes HyperFrames check and can export a valid
local MP4 through Studio. Preserve human visual edits, cue text/timing and audio.
No publication, translation rewrite, database changes or dependency upgrade.

Evidence: native export reports missing seek/duration after 45 seconds; preview
console rejects compiler-rewritten project inline script by CSP. Pinned CLI check
also rejects imperative media control, requestAnimationFrame, and duplicate root
compositions created by hashed HTML dependency copies. Runtime bytes match the
existing trusted pin/hash; do not weaken CSP with unsafe-inline.

- [x] Remove the competing standalone player script; let HyperFrames own timed media.
- [x] Keep archived HTML dependencies out of root composition discovery, preserving bytes.
- [x] Test materialization and guard behavior; preserve/capture current native edits via CAS.
- [x] Verify the pinned check and actual export; update operational docs with evidence.

Local backup: backups/review-g3-export-20261110-2136/g3.

Verified completion: native Studio exported `g3_2026-11-10_21-51-37.mp4`.
ffprobe reports H.264 1024×576 at 30fps, AAC audio, 189.666667 seconds,
69,715,996 bytes. A frame at 141 seconds confirms captions and translucent box.
Runtime preview versus producer trailing newline and render bridge hashes were
independently checked against actual producer-served scripts. Forty focused tests
and full Ruff pass. Remaining workflow translation uncertainties were unchanged.
