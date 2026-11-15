---
title: Plan bù backend cho thiết kế Stitch
date: 2026-11-12
summary: "Lập plan 8 phase cho 11 hạng mục thiết kế dashboard bị bỏ vì thiếu backend; 2 hạng mục đề xuất không làm"
---

# Plan bù backend cho thiết kế Stitch

## What happened
Sau khi restyle dashboard theo Stitch, lập plan `plans/261112-1623-dashboard-backend-gaps` (8 phase, khoảng 41h) cho các phần bị bỏ vì API chưa có dữ liệu. Plan được viết sau khi khảo sát code, chưa qua red-team hay validate.

## Phát hiện đáng nhớ
- Identity MinIO `downloader` không đọc được `review/*`, nên bảng cue phải đi qua một bảng DB do worker review ghi, cùng mẫu với `review_status`.
- Image downloader không có ffmpeg; thumbnail nên cắt trên máy host từ MP4 nguồn thay vì thêm request tới CDN Douyin.
- Thời gian render chưa được lưu (`final_renders` không có thời lượng), nên ước tính render cần thêm một cột.
- Không có bộ quét kênh tự động; chu kỳ đồng bộ kênh chỉ làm được nếu mở rộng phạm vi.
- `scripts/` và `ecosystem.config.cjs` không có cloudflared, nên chỉ báo tunnel cần một URL probe cấu hình ngoài.

## Decision
Hai hạng mục đề xuất không làm: chu kỳ đồng bộ kênh (ngoài phạm vi) và nút Tải lại worker (mở đường điều khiển máy host từ web). Bảy quyết định D1 đến D7 nằm ở `plan.md`, chờ người dùng xác nhận.

## Next steps
Người dùng chốt D1 đến D7, chạy `/ak-plan red-team` rồi `/ak-cook plans/261112-1623-dashboard-backend-gaps/plan.md`.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
