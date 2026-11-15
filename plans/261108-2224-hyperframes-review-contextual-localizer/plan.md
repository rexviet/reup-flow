---
title: "HyperFrames review và Localizer có ngữ cảnh"
description: "Bàn giao project HyperFrames bền vững để review thủ công, giữ Localizer single có ngữ cảnh và tạo lại giọng theo từng cue mà không tự render/publish."
status: in-progress
priority: P1
effort: 15d
branch: feat/localization-dubber-code-mode
tags: [feature, backend, frontend, api, auth, database]
blockedBy: []
blocks: []
created: 2026-11-08
---

# HyperFrames review và Localizer có ngữ cảnh

## Outcome

Run localized mới kết thúc ở `ready_for_review` hoặc `needs_human` với project review mở trên cùng máy Mac: companion local sửa text/timing, xuất SRT và revoice; HyperFrames Studio preview/chỉnh style. Luồng mới không render MP4 cuối, không ghi `output/`, không tự publish.

## Quyết định đã xác nhận

| Câu hỏi | Lựa chọn | Quyết định |
|---|---|---|
| “Bạn sẽ mở HyperFrames Studio trên cùng máy Mac đang chạy dự án, hay từ thiết bị khác? Điều này quyết định cách mở Studio và truy cập media.” | Cùng Mac / LAN-Tailscale | Cùng Mac, loopback only. |
| “Khi ‘Tạo lại giọng câu này’ tạo ra audio dài hơn khoảng trống của câu, bạn muốn xử lý thế nào?” | Tăng tốc ≤1,15× rồi giữ clip cũ / không tăng tốc và giữ clip cũ | Thực thi `atempo` ≤1,15×; vẫn tràn thì giữ audio active cũ và caption draft/mismatch; cue trống không clip cũ tiếp tục trống. |
| “Bạn muốn bố trí phần review thế nào, khi Studio nguyên bản chưa lưu đầy đủ text/timing và chưa có nút tạo lại giọng?” | Companion cạnh Studio / patch Studio all-in-one | Companion local; không fork Studio. |

## Invariants

- Bắt đầu với wrapper pin `hyperframes@0.8.77`; Phase 1 chứng minh đúng pin, có thể thử một version mới được pin chính xác nếu cần và ghi bằng chứng. Chỉ dừng/replan khi full scope đòi đổi UX, maintained fork/provider hoặc không thể giao, không vì version number thông thường.
- Delivery mode ghi trong input; starter canary/rollback qua LOCALIZATION_DELIVERY_MODE, input cũ mặc định legacy_render.
- Contract additive; `EditSpec`, render/cache cũ và `PublishResult.review_key` MP4 không đổi nghĩa. History cũ đi nhánh legacy qua patch marker mới.
- Downloader/Workspace vẫn read-only với Temporal; downloader không đọc `review/`. Browser/project bytes không chứa MinIO, DB hoặc VoiceStudio credential.
- Bridge dùng identity review-only và internal Temporal, không có provider/DB secrets; host launcher mở Studio; chỉ bridge port và Studio port được bind loopback cố định. Host không giả định `temporal:7233` hay service DNS.

## Phases

| # | Phase | Depends | Effort | Status |
|---|---|---|---:|---|
| 1 | [Chứng minh tương thích Studio](./phase-01-start.md) | — | 1.5d | Pending |
| 2 | [Localizer có ngữ cảnh và câu unresolved](./phase-02-contextual-localizer-and-empty-lines.md) | 1 | 2.5d | Pending |
| 3 | [Review bundle bền vững và workflow không render](./phase-03-review-project-and-workflow.md) | 2 | 4d | Pending |
| 4 | [Companion round-trip và revoice từng cue](./phase-04-studio-editing-and-line-voice.md) | 3 | 4d | Pending |
| 5 | [Migration, auth, UI entry và rollout](./phase-05-validation-and-rollout.md) | 4 | 3d | Pending |

## Ownership và success

Thực thi tuần tự `1 → 2 → 3 → 4 → 5`, không edit song song. Phase 2 sở hữu Localizer/audio semantics; Phase 3 manifest/storage/workflow; Phase 4 bridge/companion/revoice; Phase 5 migration activation/downloader/web/docs.

- [ ] Project thật mở/lưu/mở lại; text/timing/SRT round-trip bằng stable cue ID, visual style không bị regenerate xóa.
- [ ] Không auto render/publish; `ready_for_review`/`needs_human` có `project_key`, không `output_key`.
- [ ] Chỉ explicit `unresolved` được rỗng; zero TTS/SRT nhưng vẫn có ID/timing/reason và BGM/silence, không original vocal.
- [ ] Revoice một cue dùng provider/cache/queue hiện có, CAS revision và actual `atempo`; lỗi/tràn/concurrency không phá clip/timing active.
- [ ] Bundle copy mọi dependency khỏi `work/`/`cache/` vào generation-scoped immutable assets/manifest trong `review/`, durable HEAD cập nhật CAS; work source cũng cô lập generation từ producer; legacy replay/Vbee pass.

## Nguồn và rollback

[Thiết kế accepted](../brainstorm/261108-2221-hyperframes-review-contextual-localizer.md) · [Nghiên cứu HyperFrames](../reports/researcher-261108-2224-hyperframes-integration.md) · [Scout Localizer/audio](../reports/researcher-261108-2224-localizer-audio-contracts.md)

Rollback: starter default về legacy_render cho run mới; giữ worker hiểu cả hai branch cho history đã start, HEAD rollback bằng CAS; host launcher dừng đúng Studio PID. DB down chỉ sau backup và khi không còn consumer mới.

## Câu hỏi còn mở

Không còn quyết định sản phẩm. Phase 1 so sánh blur/opacity/font với ảnh tham chiếu và ghi evidence; Phase 5 ghi lại cửa sổ canary theo quy trình vận hành hiện có.

## Red Team Review

Ngày 2026-11-09: user đã duyệt áp dụng cả 7 findings (2 Critical, 4 High, 1 Medium); đã sửa đầy đủ phase 1–5. [Adjudication và consistency sweep](./review-validation.md).

## Validation Log

Ba quyết định sản phẩm đã xác nhận; findings được áp dụng; bước kế tiếp là triển khai từ Phase 1 và chứng minh technical gates. [Câu trả lời, source checks và các gate chưa chạy](./review-validation.md).

Execution 2026-11-09: Phase 1 spike implemented; stable-style and companion SRT gates pass. Studio Host/Origin rejection and full browser close/reopen remain unproven, so Phase 1 is not accepted and Phase 2 has not started. See [execution evidence](../reports/cook-261109-2018-hyperframes-phase-01.md).

Subsequent user decision 2026-11-09: explicitly authorized Phase 2 implementation despite
the open Phase 1 dependency, while keeping review delivery disabled. Phase 1 remains
unaccepted; this exception does not authorize rollout or claim its gates passed.

Phase 2 execution completed under that exception: contextual status/reason, explicit
omission propagation and legacy replay verified; see [Phase 2 evidence](../reports/cook-261109-2040-contextual-localizer-phase-02.md).
Phase 1 remains unaccepted and review delivery remains disabled. Remaining work is
Phase 3–5 plus the outstanding Studio gates, outside this invocation.


Phase 3 execution completed 2026-11-09: generation-isolated preparation,
immutable review assets/manifests, durable HEAD/CAS, project publication and
no-render workflow delivery are implemented and verified. See
[Phase 3 evidence](../reports/cook-261109-2113-hyperframes-phase-03.md).
Remaining delivery work is Phase 4–5 and the open Phase 1 Studio compatibility gates.
The default delivery mode remains legacy, and the production migration has not been applied.

Phase 4 implementation is present as of 2026-11-10: companion editing, session protection,
HEAD/CAS projection recovery, guarded launcher and single-cue revoice passed local checks.
See [Phase 4 evidence](../reports/cook-261110-0620-hyperframes-phase-04.md).
Phase 4 acceptance remains open for native Studio security/round-trip and the dedicated
live smoke. The launcher fails closed; default legacy delivery and production schema
remain unchanged. Phase 5 is outside this invocation.

Phase 5 implementation is present as of 2026-11-10: transactional migration recovery,
read-only review discovery/API/UI, synchronized terminal consumers, single-generation
canary CLI and the gated rollout runbook. See [Phase 5 evidence](../reports/cook-261110-0658-hyperframes-phase-05.md).
Native Studio still fails the Host/Origin gate, and dedicated live provider/Studio
acceptance remains open. No production migration/canary/activation was performed;
read-only checks confirm legacy mode and no production `project_key` column.
The overall plan remains in progress.

Subsequent user-authorized live validation later on 2026-11-10 applied the additive
schema after verified backup/restore, deployed compatible localization binaries and
completed one new review-project generation of the latest video. It is `needs_human`,
with valid HEAD/assets, contextual inference and no final output. Backend cached revoice
passed; native Studio acceptance remains blocked. Default dispatch stays legacy.
See [live canary evidence](../reports/ops-261110-1523-voicestudio-review-canary.md).
