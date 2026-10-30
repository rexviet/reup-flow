# Phase 05 (Localizer) re-review, range 4b353ba..HEAD (b97ed80)

Verdict: PASS (no unresolved Critical or High). One Medium and several Lows are new or remain.
Skill `backend-ai-toolkit:code-review` was invoked via the Skill tool; spec, quality and adversarial stages applied.

Evidence run:
- `cd services/localization && uv run pytest -q`: 510 passed, 3 skipped, 6 deselected.
- `bash services/localization/tests/mutations/phase-05.sh`: 53 caught, 2 controls green (matches manifest `localization:05:53:2`), tree clean afterwards (`git status` empty). The full `run-mutations.sh 'localization/*'` was not run.
- No process of mine is left running. A `temporal-sdk-python ... server start-dev --port 51107` (started 19:46, not by me) belongs to another session and was left alone.

## 1. Are the claimed fixes real

| Claim | Verdict | Basis |
|---|---|---|
| H1 hang | Fixed | `ConversationSizeGuard.before_model_request` runs on every model request (pydantic-ai `_apply_before_model_request`), validator retries included. A non-retryable `ApplicationError` raised from workflow code fails the workflow: test asserts `FAILED`, `cause.type == "PayloadTooLarge"`, under 10 s and 15 s, and `answer_attempts == 1` (the retry request is the one blocked). `_localize` only catches `ActivityError`, so it is not swallowed. |
| 192 KiB measures the real request | Adequate | I encoded a real `_RequestParams` (messages with the instructions, a 60-line CJK page, tool definitions) through `PydanticAIPayloadConverter`: 37,720 bytes against 33,674 from `dump_json(messages)`. The non-message overhead is about 4 KB, far inside the 64 KiB margin. `payload_size` of bytes is `len`, and both sides emit raw UTF-8, so CJK is not undercounted. |
| Tool activities | Not a hang path | `TemporalRunContext` excludes `messages` from tool activity payloads. An oversize tool result fails in the activity (non-retryable `PayloadTooLarge`, listed in `_PERMANENT`) and then the next request trips the guard. |
| M2 | Fixed with a residue | `glossary_upsert` refuses a `term_zh` not present (NFKC) in the transcript. Speech can still induce a mapping for a term that is present. Hardening, not closure. |
| M3 | Fixed | Stored term is returned inside `wrap_untrusted`, which escapes `<` `>` `&`. Mutation caught. |
| M4 | Fixed by instruction, weakly | Instructions now say to call the tool once and what it returns. They give no instruction on what to do with the overlap list. See L2. |
| M5 | Fixed with a contract gap | See M-1 below. Zero duration was already covered: `probe_duration_ms` raises `PermanentProviderError` on an unmeasurable length (verified with a header-only WAV), so no ZeroDivisionError. |
| Lows (vi_syllables, estimate_duration) | Fixed | Ran: "số 007" 4, "-5 độ" 3, "5-10" 2, "COVID-19" 4 (not negative), "0912345678" 10, "giảm -5%" 5. `estimate_duration` raises `ModelRetry` over 60 texts. |

## 2. Are the new mutations load-bearing

Yes for the structural guards. The size guard mutation (`if size > MAX_CONVERSATION_BYTES` to `if False`) turns the retry-growth test red, and the scenario really exceeds 256 KiB (about 115 KB read plus 59 x 2.8 KB wrong answer), so without the guard it would be the codec loop, not a spurious failure. Segment cap, provenance, wrapped term, estimate cap and voice-range guards are each caught by an outcome assertion (rows, reply text, stored rows), not by a spy.

Gaps (Low, see L3): constants are not pinned, and the instructions mutation is a string-presence check.

## 3. New findings

### Medium

M-1. Voice-rate upper bound contradicts the voice contract and fails late. `activities/voice_rate.py:36-37,118-125`, `contracts/voice.py:31,36`.
`VoiceStudioSettings.speed` and `OpenAICompatSettings.speed` accept 0.25 to 4.0, but the measured rate is refused outside 2 to 8 syllables per second. A normal Vietnamese voice reads about 5 to 6 syllables per second, so `speed` of about 1.4 or more (a common choice for dubbing Chinese into a slot) is a valid config that the activity rejects with a non-retryable `ImplausibleVoiceRate`; so is `speed` at or below about 0.35. The failure comes from `_localize_once`, after ingest, demucs, transcribe and subtitle detection have already been paid for, and it fails the workflow. It would repeat for every video of that channel.
Fix: bound the rate per unit speed (`rate / voice.settings.speed` in a plausible range), or widen the bound to what the contract allows; and validate the voice once before the expensive stages (starter or the start of the workflow). Add a test at speed 1.5 with a matching fake duration.

### Low

L-1. Non-retryable on a possibly transient reading. `voice_rate.py:119-125`. A TTS that returns a truncated or silent 200 once fails the whole run permanently (no retry), although the provider call is cheap and an 8-attempt retry would usually recover. A deterministic bad voice would cost about 8 short syntheses. Consider retryable with a small attempt cap, or document that a rerun is the recovery.

L-2. `read_onscreen_text` still has no actionable purpose. `agents/localizer.py` INSTRUCTIONS step 2. The model is told when to call it but not what to do with the result, and the tool returns time overlap only. Spec says "đọc cue OCR"; the limitation is disclosed, so this is a scope gap, not a hidden one.

L-3. Mutation gaps. (a) `tests/agents/test_voice_rate.py:170`: the implausible cases give 124, 20.7 and 1.0 syllables per second, so `MAX_SYLLABLES_PER_SEC` could be 20 and every test stays green; add a case at 8.5. (b) Nothing pins the `* 3 // 4` margin: set `MAX_CONVERSATION_BYTES = MAX_PAYLOAD_BYTES` and the guard test still fails the run (the scenario is about 280 KB), so no test proves a conversation between 192 and 256 KiB is refused. A fixture of about 220 KiB of messages would. (c) The instructions test checks the prompt text, not model behaviour.

L-4. Cap fires late and as a bare FAILED. `agents/temporal.py` `run_localizer`. A transcript over 150 segments (about 7 minutes of dense speech) is rejected only after ingest, demucs, STT, OCR and `voice_rate` have run; nothing upstream limits duration, and the run ends FAILED with no `runs` row state. Acceptable while phase 08 is open and the user chose windows as the next step; an early gate (at the starter or after transcribe) would save the spend. 150 against the 3-minute target (about 60 segments) leaves 2.5x headroom.

L-5. Replay safety of the new failure paths. `MAX_SEGMENTS` and the guard raise in workflow code without a `workflow.patched` gate. For a run already in flight with a conversation between 192 and 256 KiB, or more than 150 segments, replay would now diverge (workflow task failure, retried forever). No such run can exist yet (the stage has never run against a real video), and all recorded phase-04/05 histories replay green. Worth a patch gate once real runs exist.

L-6. Leading-zero numbers. `text/vi_syllables.py` `_spell_integer`. "05" in a date or time is now read digit by digit ("không năm"), one syllable more than speech ("mùng năm", "tám giờ"). The estimate errs long, which is the safe direction. Informational.

L-7. Doc drift. `phase-05-localizer-agent.md` "Đã làm" still cites `uv run pytest` 496 passed and 44 mutations; now 510 passed and 53. The `phase-05.sh` header comment does not list the new mutation groups.

## 4. Is skipping M1 defensible

Yes. Step 7 of the phase file is explicit: flag a `text_vi` contact "không có trong `src_zh`". The verification row's wording ("nếu `text_vi` chứa tên miền thì run bị `needs_human`") reads against a fixture where the domain sits in OCR text, which is not part of `src_zh`; it is ambiguous when the domain is in the dubbed speech. Flagging every contact would also flag a creator who legitimately speaks their own address. The skip is spec-compliant, and it is recorded as an open user decision with the exact switch (`contacts_missing_from_source` to `find_contacts`). Residual risk stands: an advertised domain spoken in the source is dubbed verbatim, unflagged. It needs a user decision, not a code change.

## Unresolved questions

- Should the voice range scale with `speed`, or should the contract cap `speed` to what the range supports (M-1)?
- Early gate for over-long transcripts, or go straight to 60 s windows (L-4)?
- Flag every contact in a dubbed line, or only those absent from the source (M1, user decision)?
