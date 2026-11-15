---
title: Cook admin dashboard phase 7 docs and end-to-end verification
date: 2026-11-12
summary: "Docs xong; kiểm thử tự động chạy được phần lớn; rebuild stack thật và e2e bị chặn vì Docker Desktop không khởi động được container mới, nest/web/review-bridge đang ở trạng thái Created"
---

# Cook admin dashboard phase 7: docs and end-to-end verification

## What happened
- Docs: tạo `docs/admin-dashboard.md` (giai đoạn, generation hiện tại, lý do "Cần xử lý", thao tác và điều kiện, badge sức khỏe, giới hạn, truy cập từ xa chỉ qua 5173 và Cloudflare Access, vận hành). Cập nhật `README.md` (bảng thành phần, ba tiến trình pm2, mục smoke và dashboard). `review-companion.md`, `hyperframes-review.md`, `agent-workspace.md`, `services/web/README.md` đã được sửa từ phase 5 và 6; kiểm lại không còn link hỏng.
- Đối chiếu doc với source, sửa hai chỗ sai: badge VoiceStudio và đĩa chuyển `unknown` (không phải `down`) khi worker review tắt; "Trả về" xóa dòng render của generation.
- Backup DB trước khi rebuild: `backups/pre-phase7-20261112-100550.dump` (+ `.sha256`), `pg_restore -l` đọc được.
- Build image `nest`, `web`, `review-bridge` xong, nhưng container mới không bao giờ chạy: mọi `docker start` (kể cả `docker run --network none alpine`) treo vô hạn, dockerd không chuyển yêu cầu sang containerd. VM vẫn sống (exec và healthcheck ổn, tải thấp). Hậu quả: `nest`, `web`, `review-bridge` đang `Created`, dashboard/API không lên cho tới khi Docker Desktop được khởi động lại. Không tự khởi động lại Docker Desktop vì sẽ dừng cả stack khác (`server-*`) ngoài phạm vi.

## Verification
- `pnpm -r lint` xanh; build (downloader tsc, web vite) xanh; web 77 test, extension 92 test xanh.
- Downloader jest trên role/db tạm trong container Postgres (đã xóa): 1135 xanh, 23 đỏ trong 8 suite (credential cứng `reup:reup@localhost:5432`, `compose-constraints`, `spike-skip-locked`), đúng nhóm đã biết.
- Python localization: 3443 xanh, 87 đỏ. Phần lớn do Pillow không có raqm; 9 test còn lại (localizer deps, secret canary timeout, one_request_agents x2, one_translation, prep_stage x3, review_bridge font) đỏ y hệt trên `git worktree` HEAD sạch, nên có sẵn.
- `scripts/run-mutations.sh`: downloader phase 01, 02, 04, 05, 06, 07, 12 xanh đầy đủ; phase-03 RED (hai control cookie-leak cần cổng 5432); infra và 11 COUNT (infra cần `services/renderer/Dockerfile` không tồn tại; 11 cần S3 env); localization/* COUNT vì baseline đỏ (9 test có sẵn); `renderer/phase-07` khai báo trong manifest nhưng không có file. Cây nguồn không đổi sau khi chạy.
- Không chạy: `.env` bị hook quyền riêng tư chặn (không đọc/ghi được `DASHBOARD_MEDIA_SECRET`), e2e-smoke, chín bước e2e, reload pm2, kiểm tra LAN.

## Next steps
Khởi động lại Docker Desktop (hoặc `docker compose up -d nest web review-bridge`), đặt `DASHBOARD_MEDIA_SECRET`, chạy `scripts/start-all.sh`, rồi chín bước e2e trong phase file.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
