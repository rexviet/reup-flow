---
phase: 7
title: "Docs và kiểm thử đầu-cuối"
status: in-progress
priority: P2
effort: "3h"
dependencies: [5, 6]
---

# Phase 7: Docs và kiểm thử đầu-cuối

## Goal

Tài liệu đủ để vận hành dashboard, worker review và truy cập từ xa; một lượt chạy thật từ
link Douyin tới bản cuối đã đánh dấu đăng.

## Docs

- Create `docs/admin-dashboard.md`: các giai đoạn và ý nghĩa (gồm "generation hiện tại"), các lý do "Cần xử lý", các thao tác và điều kiện, badge sức khỏe, giới hạn (không hủy run, không chạy lại một bước, nút Studio chỉ mở trên máy này).
- Phần truy cập từ xa: <!-- red-team #4 -->
  - Tunnel **chỉ** trỏ tới `http://localhost:5173` (nginx của web, proxy `/api` và `/internal`).
  - Không bao giờ tunnel: 3300 (API), 3002 (Studio), 3310 (review-bridge), 5433 (Postgres), 6379 (Redis), 9000/9001 (MinIO), 7233/8233 (Temporal), 3100 (Langfuse).
  - Bật Cloudflare Access cho hostname trước khi mở tunnel, vì `VITE_API_KEY` nằm trong bundle; điều này chỉ an toàn khi các cổng đã bind loopback (phase 6).
- Cập nhật `docs/review-companion.md`: duyệt trên dashboard thì máy tự render từ revision đã duyệt; export tay trong Studio vẫn dùng được và không còn làm project bị coi là chưa lưu; cờ khởi chạy Chrome đã kiểm ở phase 5.
- Cập nhật `docs/hyperframes-review.md` (mục tab Review và `/api/reviews`, dòng 107-111), `docs/agent-workspace.md` (tab Workspace 2D đã gỡ, API giữ cho bản 3D), `services/web/README.md`. <!-- red-team #14 -->
- Cập nhật `README.md`: bảng thành phần thêm `reup-review-worker` (pm2) và dashboard; dòng "Hai tiến trình host" (`:150`) thành ba; dòng Agent Workspace (`:206`); mục smoke gửi `localize: true`.

## Kiểm thử đầu-cuối (stack thật)

1. Tạo job tải một kênh nhỏ với "Tự localize" tắt → video vào "Chờ localize".
2. Bấm nút extension trên một video → video đó tự vào localization.
3. Chọn hai video, bấm Đưa vào localization → thấy bước con đổi dần trong "Đang localize".
4. Tắt `reup-voice-worker` giữa chừng → badge `stt/tts` đỏ, video vào "Cần xử lý" khi quá ngưỡng; bật lại → chạy tiếp. Tắt VoiceStudio → badge VoiceStudio đỏ.
5. Khi tới "Chờ duyệt": bấm Mở Studio trên dashboard → Studio mở trên máy; một project khác vừa sẵn sàng không làm Studio này bị đóng. Sửa một câu nhưng chưa lưu → Duyệt bị chặn với lý do rõ ràng. Lưu, export tay một bản trong Studio, rồi Duyệt khi Studio vẫn mở → render xong, phát được bản cuối, tải được SRT.
6. Duyệt một bản, rồi lưu revision mới trước khi worker render → render báo "Bản đã đổi sau khi duyệt".
7. Trả về một video; chạy lại video có canary (ví dụ đang ở g7) → xuất hiện g8.
8. Đánh dấu đã đăng kèm link.
9. Mở dashboard qua tunnel có Access: xem, duyệt, phát video và tải hoạt động. Từ máy khác trong LAN, các cổng 3300/5173/5433/6379 không kết nối được.

Ghi kết quả và ảnh chụp vào `plans/reports/` theo tên chuẩn của repo.

## Verification

- `pnpm -r lint && pnpm -r test && pnpm -r build` xanh; test Python của `services/localization` xanh; `scripts/run-mutations.sh` xanh.
- Kiểm link trong các docs mới hoặc đã sửa.
