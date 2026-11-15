---
phase: 8
title: "Docs và kiểm thử đầu-cuối"
status: pending
priority: P2
effort: "3h"
dependencies: [1, 2, 3, 4, 5, 6, 7]
---

# Phase 8: Docs và kiểm thử đầu-cuối

## Goal

Tài liệu phản ánh đúng phần mới, các hạng mục "không làm" được ghi kèm lý do, và toàn bộ tiêu chí của plan được kiểm trên stack thật.

## Context

- `docs/admin-dashboard.md` mô tả giai đoạn, thao tác, badge, giới hạn và truy cập từ xa; `docs/review-companion.md` mô tả worker review (heartbeat, render, Chrome).
- Plan trước (`plans/261111-1644-admin-dashboard/`) kết thúc với phase 7 chưa kiểm hết vì Docker và `.env`; đọc journal phase 7 của nó (`plans/journals/2026-11-12-cook-admin-dashboard-phase-07-docs-and-verification.md`) để biết cách rebuild và các lỗi nền đã có.
- Các lỗi có từ trước, không tính là lỗi của plan này: `compose-constraints.spec.ts` (lệch `ORIGINAL_VOICE_GAIN`, `SINGING_MODEL`), thiếu `raqm` của Pillow, chín test Python của localization, vài suite downloader cứng `reup:reup@localhost:5432`, `renderer/phase-07` có trong manifest mà không có file.
- `localization-starter` được build từ mã nguồn (`docker-compose.yml:459-462`) nên phải được build lại sau phase 6; build chỉ `nest web review-bridge` sẽ để starter chạy mã cũ.

## Tasks

1. Cập nhật `docs/admin-dashboard.md`: thumbnail (lưu ở `output/thumbs/`, không có trong danh sách khi chưa cắt), bảng cue và bộ chuyển generation (chỉ revision HEAD; generation cũ có thể "chưa có dữ liệu cue"), ước tính hàng loạt (ngưỡng 3 mẫu, chuẩn hóa theo độ dài, cộng backlog), thống kê máy host và tunnel (biến `CLOUDFLARED_METRICS_URL`, cách bật `--metrics 127.0.0.1:<cổng>` cho cloudflared; chưa đặt biến thì badge Tunnel ẩn), ưu tiên hàng đợi (không chen run đang chạy, không áp cho job kênh), bốn trang mới, phím tắt.
2. Thêm mục "Không làm" vào `docs/admin-dashboard.md`: chu kỳ đồng bộ kênh (cần bộ quét kênh, nằm ngoài phạm vi) và nút Tải lại worker (đường điều khiển máy host từ web; dùng `pm2 restart`).
3. Cập nhật `docs/review-companion.md` cho các tác vụ mới của worker (luồng thumbnail, ghi `cues` cùng `review_status`, backfill khi khởi động, ghi `render_ms`, `machine` và `tunnel` trong heartbeat, `FFMPEG_BIN`).
4. **Thứ tự triển khai** (viết vào docs và làm theo): sao lưu DB → áp các migration (phase 3, 4, 6 theo thứ tự trong bảng đăng ký của `plan.md`) bằng `scripts/apply-migrations.sh` → kiểm `scripts/migration-evidence.sql` → build lại `nest`, `web`, `review-bridge` **và `localization-starter`** (`docker compose up -d --build --no-deps nest web review-bridge localization-starter`) → reload `reup-review-worker` và `reup-review-auto-open` qua `scripts/start-all.sh`. **Rollback ngược lại:** hoàn tác `localization-starter` và `nest` về bản trước, rồi mới chạy `.down.sql` của migration tương ứng.
5. Theo dõi quy tắc tiến trình (không để tiến trình mồ côi); chỉ dừng tiến trình do mình khởi động.
6. Chạy từng tiêu chí ở `plan.md` trên stack thật, kèm kiểm trình duyệt ở 1440 và 375 px; tắt rồi bật lại `reup-review-worker` để kiểm `unknown` của máy host và tunnel; kiểm heartbeat vẫn đập đều khi luồng thumbnail đang chạy.
7. Chạy `pnpm -r lint`, `pnpm -r test`, `pnpm -r build`, test Python và `scripts/run-mutations.sh`; so với danh sách lỗi nền ở trên và báo mọi lỗi mới.

## Verification

- Mọi hạng mục trong bảng 11 của `plan.md` có kết quả "đã kiểm" hoặc "không làm, có lý do trong docs".
- Không có lỗi test hay build mới ngoài danh sách lỗi nền.
- Starter vẫn dispatch bình thường sau khi cột `priority` và grant có mặt (kiểm bằng một video thật ở hàng đợi).
- `curl` vào IP LAN của máy ở bốn cổng 3300, 5173, 5433, 6379 vẫn bị từ chối.

## Risks

- Docker từng ngừng khởi động container mới trong plan trước: nếu lặp lại, báo cáo và dừng thay vì khởi động lại Docker Desktop (ảnh hưởng project khác).
- Kiểm "Mở Studio" thật sẽ thay Studio đang mở; chỉ chạy khi người dùng đồng ý.
- Đối tượng test cũ còn trong MinIO (`output/review/7691886043717039013/g7/…`, khoảng 259 MB) cần quyền admin để xóa; không tự xóa.
