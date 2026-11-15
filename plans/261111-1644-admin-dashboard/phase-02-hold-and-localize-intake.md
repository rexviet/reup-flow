---
phase: 2
title: "Giữ video và đưa vào localization"
status: completed
priority: P1
effort: "5h"
dependencies: [1]
---

# Phase 2: Giữ video và đưa vào localization

## Goal

Video tải xong vào outbox ở trạng thái `held` trừ khi kênh hoặc chính video được bật tự
localize. Admin đưa video vào localization bằng một thao tác (đơn lẻ hoặc hàng loạt).
Nút 1-click của extension vẫn localize ngay (quyết định của người dùng, 2026-11-12).

## Context

- `markDone` lật video sang `done` và insert outbox trong cùng transaction (`services/downloader/src/db/videos.repository.ts:244-265`). Bất biến "video done luôn có dòng outbox" giữ nguyên.
- `videos` không có `job_id`; video gắn với kênh qua `sec_user_id`. Vì vậy công tắc đặt trên `profiles`, không đặt trên `jobs`.
- `POST /api/jobs` (`jobs.controller.ts:28-89`) và `POST /api/videos` (`videos/video-enqueue.controller.ts:12-16`) dùng zod để kiểm body.

## Files

- Modify: `services/downloader/src/db/videos.repository.ts` (`markDone` chọn `pending` hoặc `held`)
- Modify: `services/downloader/src/jobs/*` (body `POST /api/jobs` nhận `auto_localize?: boolean`, ghi lên `profiles`)
- Modify: `services/downloader/src/videos/video-enqueue.controller.ts` và service (body nhận `localize?: boolean`)
- Create: `services/downloader/src/dashboard/intake.repository.ts` (`localize`, `setProfileAutoLocalize`)
- Modify (consumer của hành vi cũ): <!-- red-team #12 -->
  - `services/extension/lib/api-client.js` (gửi `localize: true`), test `services/extension/test/{api-client,enqueue-video,sw}.spec.js`, `services/extension/README.md`
  - `scripts/e2e-smoke.sh` (gửi `localize: true`), `README.md` mục smoke
  - `services/downloader/test/mutations/phase-12.sh` (chuỗi khớp `markDone` mới) và dòng manifest `downloader:12:1:0` trong `scripts/run-mutations.sh`
  - `scripts/backfill-localization-outbox.sql` (chèn `state='held'`)
  - `services/downloader/test/mutations/phase-03.sh` nếu khẳng định "POST /api/jobs làm đúng ba việc" xung đột với việc ghi công tắc kênh
- Tests: `services/downloader/test/db/*`, `test/jobs/*`, `test/videos/*`, test mới cho intake

## Tasks

1. `markDone`: `state = CASE WHEN COALESCE(v.localize_on_done, p.auto_localize) THEN 'pending' ELSE 'held' END`, đọc trong cùng transaction.
2. `POST /api/jobs { url, auto_localize? }`: khi có giá trị thì cập nhật `profiles.auto_localize` trong transaction tạo job. Bỏ trống = giữ giá trị hiện tại của kênh.
3. `POST /api/videos { url, localize? }`: ghi `videos.localize_on_done`. Nếu video đã `done` và outbox đang `held` mà `localize=true` thì gọi cùng hàm `localize()` ở task 4 (một đường chuyển trạng thái duy nhất).
4. `localize(awemeIds[])`: `UPDATE localization_outbox SET state='pending', next_attempt_at=now() WHERE aweme_id = ANY($1) AND state='held' RETURNING aweme_id`. Trả về danh sách đã chuyển và danh sách bỏ qua kèm lý do. Ghi `dashboard_actions`.
5. `PATCH /api/profiles/:secUserId { auto_localize }` để đổi công tắc kênh từ dashboard.
6. Cập nhật các consumer ở mục Files.
7. Sửa drift retry: `services/web/src/api/types.ts:58` thiếu `'cancelled'` so với `jobs.repository.ts:580`. Phase 6 dùng chung một nguồn.

## Verification

- Test: kênh tắt → video done có outbox `held`; kênh bật → `pending`; video ghi đè thắng kênh.
- Test: `localize` chỉ đổi dòng `held`, chạy hai lần không đổi gì thêm, không đụng `dispatched`.
- Test (DB test): dòng `held` không bị starter claim.
- `pnpm --filter downloader test`, test extension, và `scripts/run-mutations.sh` xanh.
- `scripts/e2e-smoke.sh` chạy hết trên stack thật.

## Risks

- Đổi hành vi mặc định: mọi kênh hiện có thành tắt sau migration. Ghi rõ trong docs (phase 7) và trong form tạo job.
