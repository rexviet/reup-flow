# Studio preview loading investigation

Generation 3 source file is present (30,244,045 bytes); the video element still
references the correct asset. Preview HTML returned in 8ms. First and last byte
ranges returned HTTP 206 in 12ms and 1ms respectively, with correct MIME and ranges.
There was no evidence of slow MinIO or a need to rerun localization.

Chrome DevTools recorded the video request stalled for about 5.3 seconds before
sending; server response took 0.78ms and transfer took 4.79ms. Four Studio tabs
from this session were open against the same local server, alongside thumbnail
requests. Duplicate-tab connection contention is the working explanation, not a
claim that every delay was measured. Three obsolete session-created Studio tabs
were closed without an unsaved-changes prompt; companion and unrelated tabs were
left open. Reloading the remaining generation-3 tab immediately restored the
source image and timeline thumbnails at 00:25. Native Studio PID 70470 was reused.

No project source, caption, voice, HEAD, credentials, vendor package or service was
changed. The user's locally saved gradient edit remains in index.html. The console
also showed that Studio wraps the generated inline player and changes its CSP hash;
video restored without changing that policy, so this is a separate compatibility
finding rather than the measured network delay.
