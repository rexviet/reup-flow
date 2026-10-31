# Giai đoạn 4: Panel chi tiết video và test giao diện

Trạng thái: completed · Ưu tiên: P2 · Effort: 4h · Phụ thuộc: giai đoạn 3

## Mục tiêu

Bấm một bàn hoặc một video mở panel bên phải: tiến độ các pha, chi phí LLM theo stage, và kết quả cổng. Hoàn tất bộ test cho toàn trang.

## Việc cần làm

1. **Endpoint chi tiết:** `GET /api/workspace/runs/:awemeId/:generation` trong cùng module của giai đoạn 1. Trả các pha đã qua và đang chạy (từ lịch sử Temporal), tổng chi phí và chi phí theo `stage` (từ `llm_calls`), thời gian chạy của các activity thuộc queue `stt` và `tts` (cách suy ra thời gian GPU, đã nêu trong handoff), kết quả cổng. Cũng chỉ đọc và không trả nội dung.
2. Kiểm tra `awemeId` và `generation` bằng zod như `jobs.schemas.ts`; run không tồn tại trả 404.
3. `services/web/src/workspace/RunPanel.tsx`: tiến độ dạng danh sách pha, bảng chi phí theo stage, nhãn kết quả cổng. Thời gian GPU được ghi rõ là "ước tính từ thời gian activity, chưa phải hoá đơn".
4. Liên kết từ video sang trang jobs hiện có chỉ khi có dữ liệu để liên kết; nếu không thì thôi.
5. Bao quát test: hoàn thiện test của cả trang (bàn, bong bóng, đường nối, panel), kiểm tay toàn bộ trên một video thật, rồi chụp ảnh cho người dùng duyệt.
6. Tài liệu: thêm một mục ngắn vào `docs/` (cách mở tab Workspace, bộ từ vựng event, giới hạn "chỉ độ chi tiết theo activity") sau khi đọc tài liệu hiện có để chọn đúng chỗ.

## File sở hữu

Tạo `services/web/src/workspace/RunPanel.tsx` và test; sửa `workspace.controller.ts`, `workspace.service.ts`, `workspace.schemas.ts`, `services/web/src/api/client.ts`, `types.ts`; một tệp trong `docs/`.

## Kiểm thử

- Endpoint: run có đủ dữ liệu, run không có `llm_calls`, run không tồn tại (404), tham số sai (400).
- Panel: render mỗi kết quả cổng; chi phí bằng 0 hiển thị là 0 chứ không ẩn; nhãn ước tính GPU luôn có.
- Chạy cả bộ: `pnpm --filter downloader test`, `pnpm --filter @reup-flow/web test lint build`.

## Nghiệm thu

- Panel của một video đã hoàn tất khớp tổng chi phí `localization.llm_calls` cộng theo `aweme_id` và `generation` (cùng cách tính của handoff).
- Bốn kết quả của 8 mẫu (`published`, `skipped_ad`, `skipped_no_video`, `published_original`) đều hiển thị đúng nhãn.
- Mọi bộ test xanh; người dùng đã xem ảnh chụp và đồng ý.

## Rollback

Gỡ route chi tiết và `RunPanel`; trang quay về trạng thái giai đoạn 3.
