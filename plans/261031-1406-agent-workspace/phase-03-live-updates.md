# Giai đoạn 3: Cập nhật sống, bong bóng thoại, đường giao tiếp

Trạng thái: completed · Ưu tiên: P2 · Effort: 5h · Phụ thuộc: giai đoạn 2

## Mục tiêu

Trang tự cập nhật; bong bóng và đường nối xuất hiện đúng khi event tương ứng trong bộ từ vựng xảy ra.

## Quyết định: polling, không SSE

Người dùng nêu "polling/SSE". Chọn **polling** mỗi 2 giây bằng `usePolling` có sẵn, vì chỉ 1-3 người xem, endpoint đã có bộ nhớ đệm 2 giây, và SSE kéo theo kết nối dài qua nginx cùng việc xử lý ngắt kết nối. Nếu sau này cần độ trễ thấp hơn, SSE thay được lớp tải mà không đổi giao diện.

## Việc cần làm

1. `useWorkspaceSnapshot` (dựa trên `hooks/usePolling.ts`): tạm dừng khi tab trình duyệt ẩn; khi lỗi giữ snapshot cũ nhưng đánh dấu "cũ" kèm số giây trễ, không im lặng hiển thị dữ liệu cũ như dữ liệu mới.
2. **Suy ra thay đổi:** so snapshot mới với snapshot trước để sinh danh sách event hiển thị, nhưng dùng đúng `events` của server làm nguồn sự thật (khoá theo id/thời điểm) để không tạo event lặp giữa các lần poll.
3. **Bong bóng thoại:** mỗi event `activity_completed`, `activity_failed`, `llm_call_recorded`, `gate_decision` mở một bong bóng một dòng trên bàn tương ứng, tự tắt sau vài giây; câu chữ lấy từ `detail` của server.
4. **Đường giao tiếp:** vẽ đường nối giữa hai bàn chỉ cho event `handoff`, tồn tại vài giây rồi mờ đi. Không vẽ đường "trang trí" giữa các bàn.
5. **Nhật ký sự kiện:** một cột bên cạnh liệt kê event gần đây theo thứ tự thời gian (giữ 50 event gần nhất); đây là bản ghi bền, bong bóng chỉ là lớp nổi tạm.
6. `prefers-reduced-motion`: bỏ chuyển động, giữ nhật ký và nhãn chữ.

## File sở hữu

Tạo `services/web/src/workspace/useWorkspaceSnapshot.ts`, `Bubble.tsx`, `HandoffLine.tsx`, `EventLog.tsx`; sửa `OfficeMap.tsx`, `Desk.tsx`, `TopBar.tsx`.

## Kiểm thử

- Hook với `vi.useFakeTimers`: poll đều, tạm dừng khi ẩn, đánh dấu cũ khi lỗi, không sinh event lặp khi hai poll trả cùng event.
- Component: bong bóng chỉ xuất hiện khi có event; không có event thì không có bong bóng và không có đường nối; đường nối chỉ từ `handoff`.
- Kiểm tay trên một video đang chạy thật: đối chiếu bong bóng và đường nối với Temporal UI.

## Nghiệm thu

- Một video chạy thật từ `ingest` đến `publish` cho thấy bàn sáng đúng thứ tự, đường nối giữa các bàn liên tiếp, và bong bóng chi phí khi có dòng `llm_calls` mới.
- Tắt backend thì thanh trên cùng báo "cũ N giây" thay vì tiếp tục hiện trạng thái như mới.
- Lint, typecheck, test xanh.

## Rollback

Hoàn các sửa trong `OfficeMap`, `Desk`, `TopBar`; xoá các file mới của giai đoạn này. Trang quay về dạng ảnh chụp tĩnh của giai đoạn 2.
