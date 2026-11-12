---
title: Cook admin dashboard phase 3 dashboard read API
date: 2026-11-12
summary: "Năm route chỉ đọc /api/dashboard/*; giai đoạn là hàm thuần trên generation hiện tại (max outbox, runs, workflow mở); Temporal đọc qua reader riêng"
---

# Cook admin dashboard phase 3: dashboard read API

## What happened
- Module `src/dashboard`: `dashboard.stage.ts` (hàm thuần `computeStage`, `attentionOf`), `dashboard.repository.ts` (một câu SQL ghép videos, outbox, runs, quyết định, render, phát hành, review_status), `dashboard.service.ts`, `dashboard.health.ts`, controller với 5 route GET (`videos`, `videos/:awemeId`, `attention`, `channels`, `health`).
- `src/workspace/workspace.steps.ts` ánh xạ activity sang bước (prepare, translate, dub, review), gồm 8 activity không khớp luật catalog.
- `src/workspace/workspace.dashboard-reader.ts`: reader Temporal riêng, chỉ đọc, phân trang hết, tính cả `VbeeDubbingWorkflow`, có `workflowStatus` cho phase 4. `src/temporal/task-queue-probe.ts` đọc poller của hàng đợi.
- Env `DASHBOARD_STUCK_MINUTES` (mặc định 30) nối vào `env.ts`, compose `nest`, `.env.example`.

## Decisions
- Generation hiện tại = max(outbox, runs, workflow mở); trạng thái outbox chỉ dùng khi nó giữ đúng generation đó. Dòng `runs` thắng workflow mở.
- Lọc `stage` và `attention` chạy trong JS sau khi SQL lọc kênh, từ khóa, ngày, vì giai đoạn cần Temporal. `counts_by_stage` đếm trên tập đã lọc SQL.
- Temporal hoặc Postgres sập vẫn trả 200 kèm `sources`; video đã qua bước tải hiện `unknown: true`, không hiện "rảnh". Chi tiết video khi Postgres sập trả `video: null`.
- Chỉ cảnh báo generation cũ khi run có `project_key` (đúng điều kiện của auto-open), gộp thành một mục.
- Khóa lưu trữ của render không ra khỏi server; chỉ có `final_ready` và `srt_ready`.
- Activity lạ vẫn vào "Cần xử lý" khi thử lại hoặc quá hạn; chỉ nhãn bước con là "không rõ".

## Verification
- Review độc lập: không có lỗi nặng; sửa 3 mục medium (project_key, timeout từng describe và giới hạn song song, log khi nguồn lỗi) và 3 mục low (outbox cũ, `to` theo ngày, cache chi tiết).
- Jest dashboard, workspace, architecture, env: 342 xanh trên Postgres tạm ở cổng 5544; tsc và eslint sạch. Toàn bộ downloader: các suite đỏ còn lại là mục đã biết (credential cứng 5432, `compose-constraints`, `reviews.repository` flaky); `every-route-requires-api-key` đỏ một lần khi chạy song song nhưng xanh khi chạy riêng.
- Bản sao dữ liệu thật: hai video hiện g3 và g7, 50 dòng khoảng 30 ms; Temporal thật trả đủ 7 hàng đợi, MinIO ok; `host_workers` trống nên worker máy `down` tới phase 5.
- Chưa chạy: HTTP vào container `nest` (còn code cũ, cần build lại).
