# Enable project delivery and automatic local review

Outcome: newly dispatched extension downloads use `review_project`; this Mac opens
native Studio and companion once when a new project reaches `ready_for_review` or
`needs_human`.

Constraints: preserve recorded workflow modes and current native edits. Do not
reprocess old videos, render/publish, expose host control through HTTP, print keys,
or interrupt an open review. Use the guarded launcher and existing container DB
identity. Keep delivery activation persistent across ordinary Compose restarts.

- [x] Add read-only ready-project discovery and a persistent host watcher with deduplication.
- [x] Install a login LaunchAgent and enable current-machine delivery mode.
- [x] Verify discovery, queue/busy handling, restart deduplication and launch integration.
- [x] Update owning operational docs and record actual activation/remaining limits.

Separate incoming investigation: dialogue at 1:56 is missing before Localizer;
transcript jumps from 112918 to 123674 ms although source subtitle visibility is
115000–117500 ms. Diagnose VAD versus STT without hand-authoring translation.

Activated: starter reports voicestudio/review_project. LaunchAgent runs under GUI
user 501; polling real DB returns the existing g2/g3 with historical start time,
and no projects with the persisted first-activation timestamp. The current g3
Studio on port 3002 remains running. Automatic first opening on a genuinely new
completion has not occurred yet; queue/restart/failure cases have executable tests.

Missing-dialogue diagnosis: full-stem VAD threshold 0.5 skipped 113–123 seconds.
At 0.25 it retains a chunk covering 112218–115430 ms. Direct real STT on the
113–123-second crop returned “Oh shit!”; the recovered shorter chunk returned
“Ah!”. Threshold was lowered to keep speech eligible, but exact wording remains
a review concern. Existing g3 captions/audio were not hand-edited or regenerated.
