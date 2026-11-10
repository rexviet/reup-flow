# Repair translation evidence and caption fit

Outcome: repair the current canary's mistranslations against source frames and make
every caption fit its subtitle box with no clipping, retaining yellow/black styling.

Constraints: preserve native Studio edits, immutable history and generation identity;
use CAS for durable revisions. Back up local projection before refreshing. No final
render, default delivery flip or unrelated pipeline changes.

Latest user styling decisions: white box at 25% opacity, always visible, centered
with a baseline width of 60% (20% margins). Font stays 20px; expand the shared box
when long captions need more room, instead of shrinking text.

- [x] Read source frames and prove the ASR/translation failure.
- [x] Fit captions inside shared white centered box, always visible.
- [x] Give the translator timestamped visual source evidence and test its recovery.
- [x] Run a fresh AI localization canary and verify native preview.

User correction: fix the translating component, not hand-edit its output. The manual
repair process was stopped and its caption/audio changes were undone through CAS;
revision `42b661479d36439bbedf9db644398104` retains only the requested layout changes.
No hand-authored Vietnamese corrections will be supplied as the AI's expected output.

Delivered: generation 3 completed with AI-generated captions/audio and is open in
native Studio at 00:25. HTTP TTS timeout was separated from STT/OCR after live budget
inspection. [Evidence and remaining review limits](../reports/fix-261110-2003-source-grounded-localizer.md).

Acceptance: no literal invented interpretation of corrupt STT; unresolved evidence
is explicit. Captions wrap at fixed 20px; box expands for long lines. Corrected spoken
lines are rebuilt or clearly marked as audio mismatches. Human visual edits survive.
