---
title: Cook admin dashboard phase 6 dashboard web UI
date: 2026-11-12
summary: "services/web thành một trang dashboard với chi tiết video; gỡ Workspace và Review; cổng chỉ bind loopback, web gọi API cùng origin"
---

# Cook admin dashboard phase 6 dashboard web UI

## What happened
- `services/web`: `DashboardPage` (thanh sức khỏe poll 10s, "Cần xử lý", bảng pipeline có chip lọc, tìm kiếm, kênh, ngày, chọn nhiều, thanh thao tác hàng loạt, poll 3s và dừng khi tab ẩn), `VideoDetail` (`#/video/<aweme>`: video gốc và bản cuối từ link ký, timeline, verdict, chi phí, generation, nút theo giai đoạn, lịch sử), `ChannelsPanel` (JobForm có công tắc Tự localize và Localize ngay, job, kênh).
- Gỡ `src/workspace`, `src/reviews`, `services/downloader/src/reviews`, test liên quan và `web-layout.spec.ts`; sửa `app.module.ts`, hai test kiến trúc/auth.
- `docker-compose.yml`: bind `127.0.0.1` cho 5173, 3300, 5433, 6379; web build với `VITE_API_BASE_URL=""`. nginx tắt buffering cho media; vite dev proxy `/api` và `/internal`.
- Backend nhỏ: list thêm `stage_since`, `updated_at`, `revision`, `mismatch_count`; detail thêm `studio_request`.

## Decisions
- Duyệt chỉ một video mỗi lần (khớp phase 4); xác nhận nêu revision và số mismatch, mismatch có hộp riêng.
- `usePolling` thêm `key` (đổi bộ lọc thì fetch ngay) và `refresh`, bỏ kết quả trả về chậm hơn kết quả mới.
- Player giữ link đang chạy, chỉ đổi sang link mới khi tạm dừng, hết, hoặc lỗi, rồi tiếp tục từ giây cũ.
- Video đơn không tick "Localize ngay" thì bỏ cờ `localize` (theo kênh), không gửi false.

## Review
- Review độc lập (ak-code-review) tìm 2 High (duyệt từ danh sách không thấy revision; player kẹt khi link hết hạn) và vài Medium (poll chồng thứ tự, mất form khi Postgres chập chờn, localize false ép giữ, bản render cũ không có link, retry job không khóa, thiếu trạng thái Studio). Đã sửa hết; bỏ qua mục low về dọn selection chi tiết.

## Next steps
- Phase 7: build lại `nest` và `web`, đặt `DASHBOARD_MEDIA_SECRET`, kiểm tra trình duyệt với stack thật (cả màn hình hẹp, tua video) và `curl` vào IP LAN bị từ chối.
- `compose-constraints.spec.ts`, `reviews`-style credential cứng 5432 là lỗi có sẵn, không đụng.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
