---
title: Cook admin dashboard phase 2 hold and localize intake
date: 2026-11-12
summary: "Video tải xong mặc định vào outbox held; localize() là đường duy nhất held sang pending; extension và smoke vẫn localize ngay"
---

# Cook admin dashboard phase 2: hold and localize intake

## What happened
- `markDone` chèn outbox `held` hoặc `pending` theo `COALESCE(videos.localize_on_done, profiles.auto_localize, false)`, đọc trong cùng transaction với việc lật `done`.
- Mới `src/dashboard/`: `intake.repository.ts` (`localize`, `setProfileAutoLocalize`, `setLocalizeOnDone`), `PATCH /api/profiles/:secUserId`, `DashboardModule`.
- `POST /api/jobs` nhận `auto_localize` (ghi lên profile, bỏ trống giữ nguyên); `POST /api/videos` nhận `localize` (ghi override, và `true` trên video đã done/held thì gọi `localize()`).
- Consumer: extension gửi `localize: true`, `e2e-smoke.sh` cũng vậy, backfill SQL chèn `held`, mutation `phase-12.sh` khớp chuỗi mới, `RETRYABLE_STATUSES` của web thêm `cancelled`.
- Test: outbox (held/pending/override, localize idempotent, không đụng dispatched), jobs/videos/profile e2e, test Python khẳng định claim của starter không thấy dòng `held`.

## Decisions
- Mutation `phase-12` giữ nguyên ý nghĩa; chỉ đổi chuỗi khớp vì câu insert giờ nhiều dòng.
- `localize=false` không thu hồi dòng đã `pending`; chỉ ảnh hưởng video hoàn tất sau đó.
- `localize()` chưa giới hạn kích thước mảng; route hàng loạt ở phase 4 phải chặn tối đa 100.

## Verification
- Downloader: tsc, eslint sạch; jest db/jobs/videos/dashboard xanh. Các suite khởi tạo AppModule hoặc mặc định `reup:reup@5432` (db.module, health, lanes.module, cookie-leak, bootstrap-failure, probe-health, every-route) không chạy được ở máy này vì credential mặc định cứng không khớp DB thật; không liên quan thay đổi.
- Mutation `phase-12` caught; `phase-03` caught trừ hai control cookie-leak (cùng lý do credential). `compose-constraints` vẫn đỏ như trước; `reviews.repository` xanh lần này.
- Extension 92/92; web tsc sạch; Python test held xanh với `LOCALIZATION_TEST_PG_PORT=5433`.
- Chưa chạy: `scripts/e2e-smoke.sh` trên stack thật (container `nest` còn chạy code cũ, cần build lại và video thật).
