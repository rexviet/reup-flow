---
phase: 1
title: "Phần chỉ cần giao diện"
status: pending
priority: P2
effort: "3h"
dependencies: []
---

# Phase 1: Phần chỉ cần giao diện

## Goal

Làm các hạng mục 6, 9 và 10 của thiết kế, vốn không cần backend: đếm ngược tự làm mới, thanh phím tắt, badge Localhost/Tunnel.

## Context

- `services/web/src/components/AppShell.tsx` đã có thanh trên và sidebar; nhãn "Bảng 3s / Sức khỏe 10s" là khoảng poll thật (journal restyle).
- `usePolling` ở `services/web/src/hooks/` giữ dữ liệu cũ khi lỗi; `useDashboard.ts` giữ thời điểm health cập nhật (`generatedAt`).
- Duyệt chỉ bật khi chọn đúng một video và luôn qua hộp xác nhận (`ConfirmDialog.tsx`). Phím tắt không được bỏ qua bước này.

## Thiết kế

- **Đếm ngược (6):** hiện "Cập nhật Ns trước" tính từ lần poll thành công gần nhất, kèm trạng thái "Dữ liệu cũ" khi poll lỗi. Không đếm ngược sang lần poll kế (khoảng poll đổi khi tab ẩn nên số đó sẽ sai).
- **Phím tắt (9):** `/` hoặc `Ctrl/Cmd+K` focus ô tìm kiếm; `Space` chọn dòng đang focus; `D` mở hộp xác nhận Duyệt khi đúng một video được chọn và hợp lệ; `Esc` đóng hộp. Bỏ qua khi đang gõ trong ô nhập. Thanh gợi ý chỉ liệt kê phím đã làm thật, ẩn trên màn hình hẹp (không có bàn phím).
- **Badge môi trường (10):** `location.hostname` là `localhost`/`127.0.0.1` thì "Localhost", ngược lại "Tunnel". Nhãn "Single Admin" chỉ là chữ tĩnh trong thiết kế; bỏ, vì không phản ánh dữ liệu và dễ gây hiểu nhầm là có đăng nhập.

## Files

- Modify: `services/web/src/components/AppShell.tsx`, `services/web/src/dashboard/DashboardPage.tsx`, `services/web/src/dashboard/PipelineTable.tsx`
- Create: `services/web/src/dashboard/useShortcuts.ts`, `services/web/src/components/RefreshIndicator.tsx` và test cạnh từng file

## Tasks

1. Thêm `RefreshIndicator` nhận `lastUpdatedAt` và `stale`, cập nhật mỗi giây, dùng ở thanh trên.
2. Viết `useShortcuts` (đăng ký một lần ở `DashboardPage`, bỏ qua khi `event.target` là input/textarea/contenteditable).
3. Nối `D` vào đường mở hộp xác nhận Duyệt hiện có, không gọi API trực tiếp.
4. Thêm badge Localhost/Tunnel vào `AppShell`.
5. Cập nhật test: phím tắt bị bỏ qua khi đang gõ; `D` không làm gì khi chọn nhiều hơn một video; badge theo hostname (đặt `window.location` giả trong test).

## Verification

- `pnpm --filter web lint && pnpm --filter web test && pnpm --filter web build` xanh.
- Kiểm tra trình duyệt ở 1440 và 375 px: thanh phím tắt ẩn trên màn hình hẹp, focus ring rõ.

## Risks

- Phím `D` chạm tới thao tác tốn tiền: bắt buộc qua hộp xác nhận, có test.
- Xung đột phím với trình duyệt hoặc trình đọc màn hình: chỉ dùng phím đơn khi không focus ô nhập, không đè `Ctrl+K` nếu trình duyệt đã chiếm (kiểm tra thủ công).
