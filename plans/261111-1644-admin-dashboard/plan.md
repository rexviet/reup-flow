---
title: "Admin dashboard theo dõi tiến độ từng video"
description: "Gộp tab Jobs, Review và Workspace thành một dashboard theo dõi mỗi video từ lúc tải tới lúc phát hành, kèm duyệt rồi tự render bản cuối."
status: pending
priority: P1
effort: 53h
branch: feat/localization-dubber-code-mode
tags: [feature, frontend, backend, database]
blockedBy: []
blocks: []
created: 2026-11-11
---

# Admin dashboard theo dõi tiến độ từng video

## Overview

Một trang duy nhất trong `services/web` cho một admin trên máy này. Mỗi video là một dòng,
đi qua các giai đoạn Tải → Chờ localize → Đang localize (chuẩn bị, dịch, lồng tiếng, review) →
Chờ duyệt → Render bản cuối → Phát hành. Dữ liệu ghép từ bảng downloader, `localization.*`,
Temporal (chỉ đọc) và một worker review chạy trên máy. Tab Workspace 2D bị gỡ; API
`/api/workspace/*` giữ lại cho bản 3D đang thiết kế.

## Quyết định đã chốt (với người dùng, 2026-11-11 và 2026-11-12)

- Một người dùng, chạy local. Truy cập từ xa qua cloudflared tunnel, bảo vệ bằng Cloudflare Access; không tự viết đăng nhập. Mọi cổng host bind loopback.
- Downloader giữ Temporal **chỉ đọc**: không có nút hủy run localization (dùng Temporal UI).
- Video từ job tải kênh **mặc định bị giữ lại** (`held`); mỗi kênh có công tắc tự localize, mặc định tắt. Nút 1-click của extension vẫn localize ngay.
- **Duyệt rồi tự render**: duyệt gắn với revision admin đang xem → worker pm2 trên máy dựng đúng revision đó ra thư mục tạm, chạy `hyperframes render`, upload MP4 và SRT lên MinIO.
- Nút **Mở Studio** là nút thật: dashboard ghi yêu cầu, worker trên máy mở Studio.
- Phát hành ở MVP là đánh dấu tay + link bài đăng; trạng thái thiết kế sẵn để activity đăng qua MCP ghi vào sau.
- Chạy lại = tạo generation mới (`max(outbox, runs, workflow mở) + 1`) cho cả run; workflow không hỗ trợ chạy lại một bước từ bên ngoài.

## Ngoài phạm vi

Tự quét kênh, thống kê chi phí/năng suất, thông báo Telegram/Slack, tự đăng qua MCP,
hủy run localization, chạy lại một bước riêng lẻ, nhúng Studio vào dashboard, giảm quyền
DB của downloader.

## Phases

| # | Phase | Status | Phụ thuộc |
|---|-------|--------|-----------|
| 1 | [Data model và migration](./phase-01-data-model.md) | Completed | — |
| 2 | [Giữ video và đưa vào localization](./phase-02-hold-and-localize-intake.md) | Completed | 1 |
| 3 | [API đọc cho dashboard](./phase-03-dashboard-read-api.md) | Completed | 1 |
| 4 | [Thao tác và media](./phase-04-dashboard-actions-and-media.md) | Completed | 2, 3 |
| 5 | [Worker review trên máy](./phase-05-final-render-worker.md) | Completed | 1 |
| 6 | [Giao diện dashboard](./phase-06-dashboard-web-ui.md) | Completed | 3, 4 |
| 7 | [Docs và kiểm thử đầu-cuối](./phase-07-docs-and-end-to-end-verification.md) | In progress (docs done; e2e blocked) | 5, 6 |

Phase 3 và 5 có thể chạy song song sau phase 1 (khác service, khác file).

## Success Criteria

- [ ] Mở `http://localhost:5173` thấy một trang dashboard; không còn tab Workspace và Review riêng.
- [ ] Mỗi video hiện đúng generation hiện tại và giai đoạn hiện tại, kể cả bước con khi đang chạy trong Temporal và generation do canary tạo.
- [ ] Video từ job tải kênh nằm ở "Chờ localize" cho tới khi admin bấm đưa vào localization.
- [ ] Duyệt một review project tạo ra MP4 và SRT của đúng revision đã duyệt trong MinIO, phát và tải được từ dashboard, kể cả khi Studio đang mở.
- [ ] Hộp "Cần xử lý" liệt kê đủ các loại lỗi/kẹt ở phase 3.
- [ ] Badge sức khỏe phản ánh đúng khi tắt `reup-voice-worker`, VoiceStudio, `reup-review-worker` hoặc MinIO.
- [ ] Từ máy khác trong LAN không kết nối được 3300/5173/5433/6379; qua tunnel có Access dashboard chạy đủ.
- [ ] `pnpm -r test`, test Python của localization, `scripts/run-mutations.sh` và `pnpm -r build` đều xanh.

## Dependencies

- Stack compose và các worker pm2 hiện có (`scripts/start-all.sh`, `ecosystem.config.cjs`).
- HyperFrames CLI pin trong `services/localization/spikes/hyperframes-review` (v0.8.77).

## Red Team Review

### Session — 2026-11-12
**Findings:** 15 (14 accepted, 1 rejected; #10 accepted một phần)
**Severity breakdown:** 4 Critical, 9 High, 2 Medium
**Reviewers:** Security Adversary, Failure Mode Analyst, Assumption Destroyer, Scope & Complexity Critic. Người dùng duyệt từng phát hiện.

| # | Finding | Severity | Disposition | Applied To |
|---|---------|----------|-------------|------------|
| 1 | Generation hiện tại không phải `outbox.generation` (canary g3/g7); `rerun` âm thầm không chạy; không chạy lại `rejected` | Critical | Accept | Phase 3, 4, 7 |
| 2 | `renders/*.meta.json` làm project bị coi là chưa lưu mãi | Critical | Accept | Phase 5, 7 |
| 3 | `<video>` và link tải không gửi được API key | Critical | Accept | Phase 4, 6 |
| 4 | Tunnel không chạy (URL API tuyệt đối, CORS); cổng mở trên mọi interface | Critical | Accept | Phase 6, 7 |
| 5 | Duyệt không gắn revision; pipeline tự ghi được quyết định | High | Accept | Phase 1, 4, 5, 6 |
| 6 | Cổng "Studio đang mở" gây vòng claim/trả lại | High | Accept (thay bằng render từ revision bất biến, bỏ cổng) | Phase 5 |
| 7 | Phát hiện workflow mở giới hạn 10, thiếu Vbee, xóa cache; Temporal sập vẫn cho chạy lại | High | Accept | Phase 3, 4 |
| 8 | Worker chưa chịu crash: kill_timeout, lấy lại lease vô hạn, không có token claim, file tạm, đĩa đầy | High | Accept | Phase 1, 5 |
| 9 | Migration: evidence, test helper, OWNER, GRANT vô nghĩa với superuser | High | Accept | Phase 1 |
| 10 | HTML chạy trong Chrome trên máy với credential trong env | High | Accept một phần (env tối thiểu, process group, kiểm sandbox; không tạo user macOS riêng) | Phase 5, 7 |
| 11 | `Storage` không đọc được `output/`; backend local; `init.sh` reset secret | High | Accept | Phase 4 |
| 12 | Giữ video mặc định làm hỏng smoke, mutation phase-12, backfill, extension | High | Accept | Phase 2, 7 |
| 13 | Test ánh xạ activity → bước con không viết được; 8 activity chưa ánh xạ | High | Reject (người dùng) | — |
| 14 | Gỡ `/api/reviews` sót consumer; `HOST_APPS`; env mới chưa nối | Medium | Accept | Phase 3, 4, 5, 6, 7 |
| 15 | Badge VoiceStudio chỉ đo poller | Medium | Accept | Phase 3, 5, 6 |

Không đưa vào bảng (bác ở bước adjudication): đề xuất bỏ lịch sử thao tác và rút gọn trạng
thái phát hành, vì người dùng đã đồng ý cả hai ở MVP.

### Whole-Plan Consistency Sweep
- Files reread: plan.md, phase-01 … phase-07.
- Decision deltas checked: 9 (current generation; bảng quyết định tách khỏi `final_renders`; worker đổi tên `reup-review-worker`; render từ thư mục tạm; URL media ký; bind loopback; nút Studio thật; extension localize ngay; reader Temporal riêng).
- Reconciled stale references: 3 (`host_workers('host')`, mô tả health worker, tên phase 5 trong bảng).
- Unresolved contradictions: 1 lúc kết thúc red team (test ánh xạ activity); đã giải quyết ở Validation Session 1.

## Validation Log

### Session 1 — 2026-11-12
**Trigger:** `/ak-plan validate` sau red team.
**Questions asked:** 4

#### Verification Results
- Tier: Full (guard: đã có Red Team Review có bằng chứng, chỉ kiểm claim thêm sau red team)
- Claims checked: 10 (`find` `workspace.temporal.ts:199`; `HOST_APPS` `start-all.sh:52`; `applied_migrations` `apply-migrations.sh:63`; `MIGRATION_NAMES` `test/support/db.ts:20`; `replaceStudio` export `review-auto-open.mjs:56`; `VOICESTUDIO_URL` `start-all.sh:53`; `LOC_S3_*` trong `docker-compose.yml`; `/api/reviews` ở `every-route-requires-api-key.e2e-spec.ts:119`; `reviews.module` ở `http-lane-boundary.spec.ts:231`; `mismatch` `review_bridge/models.py:33`)
- Verified: 10 | Failed: 0 | Unverified: 0

#### Questions & Answers

1. **[Scope]** Phát hiện #13 đã bỏ, nhưng phase 3 vẫn ghi một test không viết được và 8 activity mới sẽ hiện bước con "không rõ". Xử lý thế nào?
   - Options: Ánh xạ 8 activity, sửa test | Giữ nguyên chữ trong plan | Bỏ bước con
   - **Answer:** Ánh xạ 8 activity, sửa test
   - **Rationale:** Bước con đúng là yêu cầu của dashboard; test dùng danh sách viết tay viết được ngay.
2. **[Architecture]** `reup-review-auto-open` có thể đóng Studio admin vừa mở từ dashboard. Giữ auto-open thế nào?
   - Options: Chỉ mở khi Studio đang trống | Tắt auto-open | Giữ như cũ
   - **Answer:** Chỉ mở khi Studio đang trống
   - **Rationale:** Tránh mất phiên Studio đang làm; project mới vẫn hiện trên dashboard.
3. **[Assumptions]** Có thiết bị nào khác trong LAN dùng 5173/3300/5433/6379 không?
   - Options: Không có | Có
   - **Answer:** Không có
   - **Rationale:** Xác nhận bind loopback không làm hỏng gì.
4. **[Tradeoffs]** Chất lượng render mặc định?
   - Options: delivery | looks | Giống lần export tay gần nhất
   - **Answer:** delivery
   - **Rationale:** Bản cuối để đăng; chấp nhận file lớn và render lâu hơn.

#### Confirmed Decisions
- Ánh xạ bước con: thêm 8 activity, test dùng danh sách viết tay.
- Auto-open: chỉ mở khi không có Studio nào đang mở.
- Bind loopback: giữ như plan.
- Render: `-q delivery`, FPS theo composition.
- Giả định (không hỏi): giữ mọi bản render theo revision trong MinIO, không dọn ở MVP.

#### Impact on Phases
- Phase 3: đoạn ánh xạ activity → step và test.
- Phase 5: cờ `-q delivery`; sửa `openNext` và `scripts/tests/review-auto-open.test.mjs`; dùng `replaceStudio` đã export.
- Phase 7: bước kiểm thử 5 thêm kiểm tra auto-open không đóng Studio.

### Whole-Plan Consistency Sweep
- Files reread: plan.md, phase-01 … phase-07.
- Decision deltas checked: 4.
- Reconciled stale references: 2 ("tách thành hàm export nếu chưa có" ở phase 5; test catalog không viết được ở phase 3).
- Unresolved contradictions: 0.
