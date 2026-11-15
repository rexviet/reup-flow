---
phase: 2
title: "Thống kê máy host và tunnel"
status: pending
priority: P2
effort: "6h"
dependencies: []
---

# Phase 2: Thống kê máy host và tunnel

## Goal

Dashboard hiện CPU/RAM/load của máy chạy worker và trạng thái cloudflared (hạng mục 4 và 5), lấy từ heartbeat của worker review trên máy.

## Context

- `services/localization/src/localization/review_worker/heartbeat.py`: `build_info` ghi `disk_free_bytes`, `disk_total_bytes`, `voicestudio`, `current`, `paused_reason` vào `localization.host_workers.info` (jsonb) mỗi vòng. Thêm khóa vào `info` **không cần migration**.
- Heartbeat và quét chạy **cùng một luồng chính** (`main.py:95-121`: `beat` rồi `review_scan.scan` đồng bộ). Probe nào chậm đều kéo dài vòng và làm heartbeat quá hạn 2 phút (`dashboard.health.ts:42`). Probe VoiceStudio đã tốn tối đa 2 giây mỗi vòng (`heartbeat.py:26`).
- `services/downloader/src/dashboard/dashboard.health.ts` quyết định `unknown` khi heartbeat cũ bằng cách chặn thủ công từng khóa theo `hostAlive`, và `hostHealth`/`diskHealth` là hàm riêng của module (`dashboard.health.ts:159-178`). Phase 7 cũng cần quy tắc này nên phải export hoặc gom lại.
- Container không đo được máy host (`docker-compose.override.yml:5-8`: đường container ra host rớt khoảng 45% kết nối), nên số liệu do tiến trình trên host đo.
- Env của worker **không** thừa kế từ pm2: `scripts/lib/host-voice-env.mjs:17-27` chỉ chép `PATH`, `HOME`, `LANG` và env compose của `worker-agents`, rồi `scripts/lib/review-worker-env.mjs:10-20` thêm một danh sách khóa cố định. Biến chỉ đặt trong pm2 hay `.env` sẽ bị bỏ.
- Cloudflared chạy ngoài repo (`scripts/` và `ecosystem.config.cjs` không nhắc tới), quyết định D6.
- Không có `psutil` trong `services/localization/pyproject.toml`, nên đường chính là lệnh macOS.

## Thiết kế

- **Đo máy (`machine`)** trong worker: `{ load_1m, cpu_count, mem_used_bytes, mem_total_bytes, model }`. `load_1m` và `cpu_count` từ `os.getloadavg()` và `os.cpu_count()`. RAM đã dùng tính theo mô hình của Activity Monitor: (active + wired + compressed) từ `vm_stat` nhân kích thước trang, tổng từ `sysctl hw.memsize`; **không** dùng "Pages free", vì macOS dùng RAM trống làm cache nên số đó thường chỉ vài phần trăm. Model từ `sysctl machdep.cpu.brand_string`. Không ghi hostname (D5).
- **Probe tunnel (`tunnel`)**: gọi `CLOUDFLARED_METRICS_URL + "/ready"`, timeout 1 giây, không theo redirect, `trust_env=False`. Biến chưa đặt thì worker ghi `tunnel: { "configured": false }` (không probe); phía đọc phân biệt "chưa cấu hình" với "worker chết" nhờ khóa này. <!-- Updated: Validation Session 1 - chưa cấu hình thì ẩn badge Tunnel, không hiện unknown -->
- **Nhịp đo:** `machine` và `tunnel` chỉ làm mới tối đa mỗi 60 giây và giữ kết quả cache giữa các vòng 5 giây, để không thêm lệnh con và HTTP vào mỗi vòng. Khóa ghi kèm `measured_at`.
- **Đường biến môi trường:** `scripts/review-worker.mjs` đọc `process.env.CLOUDFLARED_METRICS_URL` tường minh (không đi qua compose, nên không rơi vào container agents nơi `127.0.0.1` là chính container), `scripts/lib/review-worker-env.mjs` kiểm tra và đưa vào `workerEnv`. `ecosystem.config.cjs` thêm khối `env` truyền biến từ môi trường pm2, không đặt mặc định. Việc kiểm nằm trong **mã**, không chỉ trong Rủi ro: chấp nhận đúng `http://` tới `127.0.0.1`, `::1` hoặc `localhost`, có cổng, không userinfo, không path; ngoài ra bỏ biến và ghi một dòng log.
- **API:** `GET /api/dashboard/health` trả thêm `host_machine` và `tunnel`, dùng chung hàm `unknown` khi heartbeat cũ (export từ `dashboard.health.ts`). Load trên 1,5 lần số nhân là `degraded`; RAM dùng trên 90% là `degraded`; `measured_at` quá 5 phút thì `unknown`. `tunnel.configured === false` thì health trả `tunnel: null`; heartbeat cũ thì `tunnel` là `unknown` như các khóa khác.
- **UI:** ô thống kê máy host ở sidebar (thay ô "M2 Max" của thiết kế) chỉ hiện khi có dữ liệu; badge Tunnel ở hàng sức khỏe **ẩn hẳn** khi `tunnel` là `null` (chưa cấu hình) và hiện `ok/down/unknown` khi đã cấu hình. <!-- Updated: Validation Session 1 -->

## Files

- Modify: `services/localization/src/localization/review_worker/heartbeat.py`, `main.py`, `config.py` (đọc và kiểm `CLOUDFLARED_METRICS_URL`)
- Modify: `scripts/review-worker.mjs`, `scripts/lib/review-worker-env.mjs`, `ecosystem.config.cjs`
- Modify: `services/downloader/src/dashboard/dashboard.health.ts` (export quy tắc heartbeat cũ), `dashboard.health.service.ts`
- Modify: `services/web/src/dashboard/HealthBar.tsx`, `services/web/src/components/AppShell.tsx`, `services/web/src/api/types.ts`
- Tests: `services/localization/tests/review_worker/` (heartbeat, kiểm URL), `scripts/tests/` (env của worker, ví dụ `review-studio-request.test.mjs`), `services/downloader/test/dashboard/`

## Tasks

1. Viết `probe_machine()` với đầu ra `vm_stat` mẫu và bộ nhớ đệm 60 giây; viết `probe_tunnel()` với transport giả.
2. Viết kiểm URL loopback trong `config.py` và trong `review-worker-env.mjs`, có test cho URL ngoài loopback, có userinfo, có path, thiếu cổng.
3. Đưa `machine` và `tunnel` vào `build_info`; khóa tunnel bị bỏ khi chưa cấu hình.
4. Export quy tắc heartbeat cũ trong `dashboard.health.ts` và dùng cho các khóa mới.
5. Cập nhật kiểu và UI; thiếu khóa thì ẩn ô thay vì hiện số 0.
6. Test: tắt worker thì `host_machine` và `tunnel` (đã cấu hình) là `unknown`; biến chưa đặt thì `tunnel: null` và badge ẩn; URL trả lỗi thì `down`; biến đặt trong pm2 thực sự tới `config.py` (test mức `workerEnv`).

## Verification

- `pytest services/localization/tests/review_worker`, test script Node và `pnpm --filter downloader test -- dashboard` xanh (DB tạm cho test cần Postgres).
- Trên stack thật: `curl` với API key vào `/api/dashboard/health` thấy `host_machine`; dừng `reup-review-worker` rồi đợi quá 2 phút thì thấy `unknown`; nếu đặt `CLOUDFLARED_METRICS_URL` thật thì thấy `tunnel`.
- Sau khi bật probe, heartbeat vẫn đập đều (khoảng cách giữa hai lần ghi `seen_at` không vượt 10 giây).

## Risks

- Đo RAM macOS: test với đầu ra mẫu, kiểm thật trên máy này; nếu số liệu `degraded` liên tục thì chỉnh ngưỡng chứ không bỏ.
- Chưa đặt `CLOUDFLARED_METRICS_URL` thì badge Tunnel ẩn; đã đặt mà cloudflared không bật cổng metrics thì badge hiện `down`; docs (phase 8) hướng dẫn bật `--metrics 127.0.0.1:<cổng>`.
