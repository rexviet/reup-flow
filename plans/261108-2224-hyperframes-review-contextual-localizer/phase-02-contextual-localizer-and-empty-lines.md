---
phase: 2
title: "Localizer có ngữ cảnh và câu unresolved"
status: pending
priority: P1
effort: 2.5d
dependencies: [1]
---

# Phase 2: Localizer có ngữ cảnh và câu unresolved

<!-- Updated: Red Team Review 2026-11-09 - recorded mode keeps legacy validation and activity scheduling unchanged -->

## Goal

Cho single Localizer phục hồi có căn cứ bằng toàn transcript, biểu diễn `translated | inferred | unresolved`, và truyền unresolved qua dubbing/audio/layout mà không fail run hoặc gọi TTS.

## Ngoại lệ phụ thuộc đã được duyệt — 2026-11-09

User chọn phương án 2: triển khai Phase 02 ngay trong khi Phase 01 chưa được chấp nhận,
giữ chế độ bàn giao review chưa bật. Ngoại lệ này chỉ cho phép làm Phase 02; không
đánh dấu gate Studio Host/Origin hoặc browser close/reopen đã pass và không cho phép
bật rollout. Starter tiếp tục tạo input mặc định `legacy_render`.

## Evidence

- Initial prompt đã có toàn transcript ([`localizer_single.py:170`](/Users/macbook/Data/reup-flow/services/localization/src/localization/agents/localizer_single.py:170)); repair hiện chỉ có câu lỗi/glossary ([`localizer_single.py:276`](/Users/macbook/Data/reup-flow/services/localization/src/localization/agents/localizer_single.py:276)).
- Blank hiện là `unusable` ([`localizer_single.py:223`](/Users/macbook/Data/reup-flow/services/localization/src/localization/agents/localizer_single.py:223)); segment chưa có review metadata ([`models.py:147`](/Users/macbook/Data/reup-flow/services/localization/src/localization/contracts/models.py:147)); `DubSegment.tts_key` bắt buộc ([`models.py:214`](/Users/macbook/Data/reup-flow/services/localization/src/localization/contracts/models.py:214)).
- TTS hash đã gồm provider/model/engine/voice/text ([`synthesize.py:73`](/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/synthesize.py:73)); reuse seam, không thêm registry/provider mới.

## Files

- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/contracts/models.py` — additive line status/reason và omitted dub defaults.
- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/agents/localizer_single.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/agents/localizer.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/agents/temporal.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/agent_context.py` — contextual repair, strict IDs, patch gate.
- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/assemble_script.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/dub_script.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/synthesize.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/assemble_audio.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/layout_captions.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/review_render.py` — propagate/skip explicit unresolved; review timing/fidelity must not auto-reroute intentional omission.
- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/agents/review_checks.py` — fidelity/timing checks nhận omitted speech có chủ đích.
- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/contracts/__init__.py` — export additive types.
- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/workflows/video_localization.py` — marker mới, flags, deterministic legacy/new branches.
- Modify existing tests under `/Users/macbook/Data/reup-flow/services/localization/tests/`: `/Users/macbook/Data/reup-flow/services/localization/tests/agents/test_one_request_agents.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/agents/test_assemble_script.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/activities/test_synthesize.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/activities/test_dub_script.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/activities/test_assemble_audio.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/activities/test_layout_captions.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/agents/test_review_checks.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/workflows/test_one_request_agents.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/replay/test_replay.py`.
- Create `/Users/macbook/Data/reup-flow/services/localization/tests/fixtures/contextual-unresolved.json` — synthetic source + metadata oracle; không dùng output-only example làm semantic oracle.

## Contract / data flow

Thêm input delivery_mode với legacy_render default, và defaulted policy flag trên LocalizerDeps/context được ghi trong history; chỉ input review_project dùng contextual/blank-aware behavior. Prompt/validator cũ giữ strict semantics cho legacy/tool histories, không đọc env ở workflow để chọn policy. Stage mới chưa được bật bởi starter trước Phase 5.

Mỗi expected ID trả `{id,text_vi,status,reason?}`. `translated`: nonempty/no reason; `inferred`: nonempty + bounded reason; `unresolved`: empty + bounded reason. Legacy nonempty defaults `translated`; legacy blank không tự hợp lệ. Missing/duplicate/unknown/schema failure luôn retry/fail.

Full transcript → answer → deterministic assessment → repair chứa failed line + bounded neighbors + global transcript context → merge only asked IDs bằng `model_copy` giữ metadata → script giữ ID/timing/source → synthesis/fit/Dubber skip unresolved → dub doc giữ ID với `tts_key=None`, duration 0/omitted → audio uses BGM; no BGM generates silence; no original vocals → layout/SRT omit blank caption nhưng review row remains.

## Steps

1. Add enum/bounded reason validators; metadata không vào spoken text/SRT.
2. Prompt cho phép `unresolved` chỉ khi thiếu căn cứ và `inferred` có reason. Source/reviewer notes tiếp tục nằm trong untrusted block.
3. Repair nhận neighbor/global context. Repair omission giữ prior valid record; initial omission/duplicate/unknown không tạo unresolved ngầm.
4. `assemble_script` giữ exact ID/timing; only explicit unresolved empty. Contact, inferred, unresolved flags coexist.
5. Filter unresolved khỏi synthesis plan, fit, Dubber pending IDs/provider calls. `commit_dub` writes omitted entry; every rewrite/model_copy preserves status/reason.
6. Mixed/all-unresolved produces BGM-only; if no BGM generate duration-correct silence. Never reintroduce original vocals or fail loudnorm/SilentAudio.
7. Legacy `EditSpec`/cover/hash behavior không đổi; existing layout chỉ skip Vietnamese empty caption. New source-driven cover thuộc Phase 3 project builder. Reviewer surfaces reason, treats omitted speech as intentional, và không đưa unresolved vào auto-retry loop.
8. New patch marker + recorded delivery policy covers behavior/order change. Markers at [`video_localization.py:73`](/Users/macbook/Data/reup-flow/services/localization/src/localization/workflows/video_localization.py:73) keep old meanings.

## Verification

```bash
cd /Users/macbook/Data/reup-flow/services/localization
uv run pytest -q tests/agents/test_one_request_agents.py tests/agents/test_assemble_script.py
uv run pytest -q tests/activities/test_synthesize.py tests/activities/test_dub_script.py tests/activities/test_assemble_audio.py
uv run pytest -q tests/workflows/test_one_request_agents.py tests/replay/test_replay.py tests/replay/test_vbee_replay.py
uv run ruff check src tests
```

| Case | Pass condition |
|---|---|
| Missing/duplicate/unknown/schema | fail/retry; no partial script |
| Inferred/unresolved | invariant valid; reason metadata only; unresolved zero TTS/caption |
| Repair omit/blank | prior valid unresolved retained; other cues unchanged |
| Mixed/all unresolved | BGM/generated silence, zero original vocal, zero blank SRT cue |
| Old/Vbee histories | replay/current Vbee behavior unchanged |

Real-model contextual evaluation waits for matching source transcript; do not invent expected Vietnamese for IDs 5/10/16/25–27 from output-only file.

## Acceptance / risk / rollback

- [x] Old JSON decodes; missing ID never becomes valid blank; metadata survives repair/Dubber.
- [x] Zero segment-synthesis calls for unresolved (voice-rate calibration có thể đã gọi TTS); all-empty stays reviewable and never `published_original`.
- [x] Replay/Vbee pass; `raqm` blocker is handled by correct image, not weakened test.

| Risk | L×I | Signal | Mitigation |
|---|---|---|---|
| Blank hides model error | M×H | missing ID succeeds | Explicit enum + exact-set validator. |
| Metadata lost | M×H | inferred becomes translated | Round-trip/model_copy tests at every writer. |
| Replay nondeterminism | L×H | command mismatch | New marker + preserved legacy branch/fixtures. |

Rollback đưa starter delivery default về legacy_render cho dispatch chưa start; không tắt/xóa marker hoặc thay input của execution đã start. Giữ additive decoder/new branch cho histories đã marked; không rewrite artifact cũ.

## Execution evidence — 2026-11-09

Đã hoàn tất Phase 02 theo ngoại lệ phụ thuộc được user duyệt. [Báo cáo triển khai và
kiểm chứng](../reports/cook-261109-2040-contextual-localizer-phase-02.md).

Localizer/audio/schema/architecture: 318 passed; workflow và replay cũ/Vbee:
422 passed; layout/reviewer trong image có raqm: 164 passed. Sửa cuối về metadata
qua JSON được kiểm lại bằng 49 unit tests và 2 Temporal workflow/replay tests.
Ruff và diff checks đạt. Starter không bật review delivery; Phase 01 vẫn chưa accepted.
Real-model semantic evaluation chờ source transcript tương ứng như đã quy định.
