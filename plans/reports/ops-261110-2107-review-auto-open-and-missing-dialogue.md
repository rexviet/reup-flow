# Local review delivery and automatic Studio opening

New VoiceStudio dispatches on this Mac now use `review_project`. The base input and
base Compose legacy defaults remain compatible; the checked-in Compose override
records the user's explicit local activation. The starter was rebuilt/recreated and
reported voicestudio/review_project. Existing workflow inputs were not rewritten.

The macOS LaunchAgent `com.reup-flow.review-auto-open` runs the host watcher at login.
Current watcher PID is 95247; its command is Node 24 running
`scripts/review-auto-open.mjs` in this checkout. It polls every five seconds through
`localization.starter.review_ready` in the starter. The CLI uses its existing DB
identity with a read-only transaction and bounded connect/statement deadlines; the
host receives numeric run identities and review statuses only. No host-control HTTP
endpoint, Docker socket mount, or secret export was introduced.

Only completions since 2026-11-10T14:13:02.940Z are eligible. Persistent state is under
`data/review-launcher/auto-open.json`. Real historical discovery returned g2 and g3;
the activation-time query returned none, so existing projects were not reopened.
Both ready_for_review and needs_human are selected only with a canonical project HEAD.
Launch confirmation waits for the guarded launcher's browser-open acknowledgment.
Dedup persists only after confirmation. A different open project queues new work.
The user's existing Studio PID 84077 on loopback 3002 was preserved. The new watcher
has not yet received a genuinely new completion, so no live automatic first-opening
claim is made. Queue/restart/failure behavior has regression tests.

A launchctl bootout/bootstrap reinstall race initially produced exit 5. Bounded retry
was added to the installer; two consecutive actual installations then succeeded.
Stopping the watcher only terminates its own child launcher, not a pre-existing review.
Logs remain local and omit provider diagnostics/keys/grants.

## Missing dialogue at 1:56

Generation 3 has no transcript segments/words from 112918 to 123674 ms. Original
subtitle visibility detection contains 115000–117500 ms, though the actual source
frame at 116 seconds has no readable text. The full separated-vocal Silero VAD at
its default threshold 0.5 produced no chunk covering this dialogue, so STT and
Localizer never received it. This is upstream of translation, captions and TTS.

A real threshold comparison retained the region at 0.25 as a 112218–115430 ms chunk.
An independent 113–123-second STT crop returned “Oh shit!” at 113980–115740 ms; the
shorter recovered chunk returned “Ah!”. Recognition is crop-sensitive and exact
wording remains uncertain. Lowered VAD threshold keeps speech eligible for ASR;
existing source-evidence and inferred/unresolved policies still govern translation.
The worker was rebuilt/recreated and verified at threshold 0.25. Generation-3
caption/audio artifacts and human edits were not manually changed or regenerated.
A first isolated OpenAPI request timed out; subsequent real OpenAPI/STT calls worked.

## Validation

- Eleven Node tests passed for launcher/watcher queueing, both states, restart dedup,
  pending browser-open acknowledgement, failed launch and failed persistence.
- Twelve focused Python tests passed for ready discovery, speech chunks and multilingual
  provider behavior. Full Python Ruff passed; Git whitespace checks passed.
- An earlier wider starter/mode check had 16 passed and 26 skipped because its local
  Postgres prerequisite was unavailable. Real read-only discovery in the deployed
  starter succeeded. Skips are not represented as passed integration tests.
- The simplifier fixed premature dedup and a launch-error child leak; parent reran
  the affected eleven Node tests successfully. No schema changes, manual production
  DB writes, old-video reprocessing, final render or publication ran.

Remaining limits: the first automatic opening on a new real completion is pending;
source wording in the missing region remains uncertain; existing g3 still contains
its old ASR gap until a separately authorized rerun creates a new generation.
