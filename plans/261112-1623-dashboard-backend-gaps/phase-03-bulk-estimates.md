---
phase: 3
title: "Ước tính chi phí và thời gian render"
status: pending
priority: P2
effort: "5h"
dependencies: []
---

# Phase 3: Ước tính chi phí và thời gian render

## Goal

Thanh thao tác hàng loạt hiện ước tính chi phí LLM và thời gian render cho các video đã chọn (hạng mục 3), tính từ lịch sử thật, chuẩn hóa theo độ dài video, và ghi rõ đó là ước tính.

## Context

- Chi phí LLM từng video đã có: `llm_calls.cost_usd`, tổng theo generation ở `DashboardRepository.llmCosts` (`services/downloader/src/dashboard/dashboard.repository.ts:269`). Truy vấn này **không lọc theo mức hoàn thành**, nên gộp cả generation lỗi, dở dang và canary.
- Thời gian render **chưa được lưu**: `localization.final_renders` (`261112-review-delivery.sql`) có `rendered_at` và `attempts`, không có thời lượng. Thời gian render phụ thuộc độ dài clip và CPU đang bận (journal phase 5 của plan trước: một render 2 phút 10 giây thành khoảng 8 phút khi máy chạy việc khác), và worker render **tuần tự từng video** (`final_render.py`: claim một dòng, một luồng render).
- Danh sách video (`GET /api/dashboard/videos`) được poll mỗi 3 giây, dùng chung truy vấn `SELECT_VIDEOS`; nếu truy vấn đó lỗi thì cả bảng, danh sách "Cần xử lý", kênh và chi tiết đều rơi về `sources.postgres: down` (`dashboard.service.ts:59-68,205`). Vì vậy mọi nguồn mới phải là nguồn `attempt()` riêng, không nhập vào `SELECT_VIDEOS`.
- Downloader chạy bằng superuser `reup` nên đọc được `localization.*`; không có role `downloader` cần cấp quyền.

## Thiết kế

- **Lưu thời lượng render:** thêm cột `render_ms integer` vào `localization.final_renders`, worker ghi khi chuyển sang `rendered` (đo từ lúc claim, gồm export và upload, vì đó là thời gian người dùng thực sự chờ). Bản ghi cũ để `NULL` và không vào mẫu.
- **Ước tính render:** tỷ lệ `render_ms / videos.duration_ms` (trung vị của N=20 lần render gần nhất có đủ hai số), nhân với độ dài từng video đã chọn, **cộng backlog**: tổng ước tính còn lại của các render đang `queued` hoặc `rendering`. Áp dụng cho Duyệt và Render lại.
- **Ước tính chi phí:** trung vị tổng `cost_usd` theo (video, generation) của N=20 generation gần nhất **đã tới `ready_for_review` hoặc muộn hơn** (loại generation lỗi và dở dang). Chỉ nhân với số video cho thao tác tạo chi phí (Chạy lại, Đưa vào localization); thao tác khác không hiện chi phí.
- **Mẫu ít hơn 3 thì ẩn.** Hiện kèm "ước tính theo N video gần nhất". Ước tính chỉ là gợi ý; thời gian chờ thật còn phụ thuộc việc khác trên máy.
- **API:** thêm `estimates` vào `GET /api/dashboard/videos` như một nguồn `attempt()` riêng với khóa `sources.estimates`, tính ở mức nhịp chậm (bộ nhớ đệm 60 giây, không tính lại mỗi lần poll 3 giây). Lỗi hoặc thiếu mẫu thì `estimates: null`, danh sách vẫn trả bình thường. Dạng: `{ llm_usd_per_video, render_ratio, render_backlog_ms, sample_llm, sample_render }`, trường thiếu mẫu là `null`.

## Files

- Create: `services/localization/migrations/<ngày>-final-render-duration.sql` và `.down.sql`; đăng ký theo bảng ở `plan.md` mục "Đăng ký migration"
- Modify: `services/localization/src/localization/review_worker/final_render.py` (ghi `render_ms`)
- Modify: `services/downloader/src/dashboard/dashboard.repository.ts` (hai truy vấn mẫu, không đụng `SELECT_VIDEOS`), `dashboard.service.ts` (nguồn `estimates`, bộ nhớ đệm)
- Modify: `services/web/src/dashboard/BulkActionBar.tsx`, `services/web/src/api/types.ts`
- Tests: `services/localization/tests/review_worker/`, `services/downloader/test/dashboard/`, `scripts/tests/apply-migrations.test.sh`

## Tasks

1. Sao lưu DB (`pg_dump`), thêm migration, kiểm down rồi up lại bằng `apply-migrations.test.sh`.
2. Worker ghi `render_ms` khi render xong.
3. Repository: truy vấn trung vị (`percentile_cont(0.5)`) có `LIMIT` mẫu, cho tỷ lệ render và chi phí; truy vấn backlog.
4. Service ghép `estimates` thành nguồn `attempt()` riêng, có bộ nhớ đệm 60 giây; lỗi chỉ làm `estimates` thành `null`.
5. UI: thanh hàng loạt chỉ hiện ước tính khi API trả giá trị, kèm số mẫu và nhãn "ước tính".
6. Test: mẫu 2 thì `null`; mẫu đủ thì đúng trung vị; render thất bại không vào mẫu; generation lỗi hoặc canary không vào mẫu chi phí; truy vấn estimates lỗi không làm hỏng danh sách (`sources.postgres` vẫn `ok`, `sources.estimates` báo lỗi).

## Verification

- Test migration và test dashboard xanh; trên DB thật `estimates` trả `null` hoặc số hợp lý (kiểm bằng `curl` có API key).
- Render thật một video (đường render có sẵn từ plan trước) và thấy `render_ms` được ghi.

## Risks

- Trung vị trên ít video vẫn lệch dù đã chuẩn hóa: ngưỡng 3 mẫu chỉ giảm phương sai, không loại bỏ thiên lệch; nhãn "ước tính" là bắt buộc.
- Thời gian đo trên máy đang rảnh dưới ước tính khi máy bận: ghi rõ trong docs, không hứa chính xác.
