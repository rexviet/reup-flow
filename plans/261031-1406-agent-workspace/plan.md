---
title: "Agent Workspace: văn phòng 2D theo dõi agent của workflow localization"
description: "Một tab mới trong services/web hiển thị văn phòng nhìn từ trên; trạng thái từng agent lấy từ Temporal và Postgres qua một endpoint đọc-only trong downloader."
status: completed
priority: P2
effort: 20h
issue: null
branch: feat/localization-dubber-code-mode
tags: [feature, frontend, backend, api]
blockedBy: []
blocks: []
created: 2026-10-31
---

# Agent Workspace

## Kết quả mong muốn

Người vận hành mở một tab "Workspace" và thấy ngay agent nào đang làm video nào, agent nào đang chờ, video nào bị cổng chặn, và chi phí LLM gần đây. Mọi thứ trên màn hình là dữ liệu thật; không có animation nào khẳng định điều hệ thống không làm.

Ý tưởng gốc: https://github.com/lucwj/ai-agent-3d-prompts (văn phòng 3D, trạng thái điều khiển mọi chuyển động). Bản này là 2D, dùng React 18 và Vite có sẵn của `services/web`.

## Quyết định đã duyệt

| Quyết định | Chọn |
|---|---|
| Giao diện | 2D nhìn từ trên, không thêm three.js |
| Nguồn dữ liệu | Thật: lịch sử Temporal + `localization.llm_calls` + `localization_outbox` |
| Phạm vi | Toàn bộ agent localization, các bước workflow (media, render, stt, tts, voice_check) và các cổng |
| Nơi đặt endpoint | Phương án A: module mới trong `services/downloader` (NestJS); web chỉ gọi API downloader qua `X-Api-Key` |

## Ràng buộc

- Không sửa workflow hay agent hiện có (nên không cần `workflow.patched`).
- Endpoint chỉ đọc. Không trả nội dung prompt, đoạn dịch hay transcript; chỉ trả id, trạng thái, thời điểm, số token và chi phí.
- Temporal không mở cổng ra ngoài. Downloader chạy trong mạng compose nên gọi được `temporal:7233`.
- Hạ tầng downloader giữ ranh giới HTTP và lane: module mới không được import `lanes/` (bài test kiến trúc `test/architecture/http-lane-boundary.spec.ts` sẽ kiểm).
- Không commit khi người dùng chưa yêu cầu.

## Phát hiện khi scout

- Downloader **chưa có client Temporal** (không có `temporalio` trong `package.json` nào). Phải thêm `@temporalio/client`, một gói thuần TypeScript trên gRPC; lõi native chỉ nằm ở gói worker nên không kéo vào.
- `localization_outbox` đã nằm trong schema của downloader (`services/downloader/src/db/schema.ts`), còn `localization.llm_calls` do Python ghi; downloader cần quyền `SELECT` trên bảng này.
- Workflow id sinh bởi `workflow_id(aweme_id, generation)` trong `services/localization/src/localization/starter/outbox_starter.py`.
- Các agent LLM chạy qua `agents/temporal.py`, nên tên activity của chúng có thể không trùng tên activity thường (`classify_video`, `review_dub`, ...). Việc này là điểm chưa biết đầu tiên, giải trong giai đoạn 1.
- Web đã chấp nhận `VITE_API_KEY` nằm trong bundle (công cụ nội bộ cho 1-3 người, xem `docker-compose.yml`). Endpoint mới không làm rủi ro này tệ hơn vì chỉ trả dữ liệu không nhạy cảm.

## Giai đoạn

| # | Giai đoạn | Effort | Phụ thuộc | Nghiệm thu |
|---|---|---|---|---|
| 1 | [Endpoint đọc-only](phase-01-workspace-endpoint.md) | 6h | — | `GET /api/workspace/snapshot` trả đúng trạng thái trên một video đang chạy thật; test xanh |
| 2 | [Trang tĩnh: bố cục và bàn](phase-02-office-layout.md) | 5h | 1 | Văn phòng hiển thị đủ khu và bàn từ dữ liệu endpoint; trạng thái trống khi không có dữ liệu |
| 3 | [Cập nhật sống, bong bóng, đường giao tiếp](phase-03-live-updates.md) | 5h | 2 | Bong bóng và đường nối chỉ hiện từ event có thật; thanh trên cùng báo nguồn và độ trễ |
| 4 | [Panel chi tiết video và test giao diện](phase-04-run-detail-and-tests.md) | 4h | 3 | Bấm một bàn hoặc video mở panel tiến độ, chi phí, kết quả cổng; lint, typecheck, test xanh |

## Bộ từ vựng event (cố định)

Chỉ những event này được phép tạo ra hiệu ứng trên màn hình:

| Event | Nguồn | Hiệu ứng |
|---|---|---|
| `activity_started` | pending activity trong Temporal | bàn sáng, agent "đang làm" |
| `activity_retrying` | pending activity có `attempt > 1` | bàn nhấp nháy màu cảnh báo, ghi số lần thử |
| `activity_completed` | activity hoàn tất trong lịch sử | bàn tắt, bong bóng một dòng |
| `activity_failed` | activity thất bại | bàn đỏ, bong bóng nêu loại lỗi |
| `llm_call_recorded` | dòng mới trong `llm_calls` | bong bóng "model, token, chi phí" |
| `gate_decision` | trạng thái run: `skipped_ad`, `skipped_no_video`, `published_original`, `needs_human` | video đi vào khu Cổng, nhãn quyết định |
| `handoff` | hai activity liên tiếp của cùng một workflow, hoàn tất rồi bắt đầu | đường nối từ bàn A sang bàn B |

Không có dữ liệu thì không có hiệu ứng. Thanh trên cùng luôn ghi nguồn (Temporal, Postgres) còn sống hay không và thời điểm cập nhật gần nhất.

## Rủi ro

| Rủi ro | Xử lý |
|---|---|
| Tên activity của agent LLM khác dự kiến, bảng ánh xạ sai | Giai đoạn 1 mở đầu bằng spike đọc lịch sử một workflow thật; bảng ánh xạ nằm ở một chỗ và có test chặn activity lạ |
| Gọi Temporal mỗi giây từ nhiều tab làm nặng server | Bộ nhớ đệm trong tiến trình khoảng 2 giây; chỉ describe tối đa `MAX_OPEN_WORKFLOWS` (10) workflow đang mở |
| Temporal hoặc Postgres chết kéo sập API downloader | Endpoint trả `degraded` kèm nguồn nào lỗi, không ném lỗi; `/health` không phụ thuộc Temporal |
| Quyền `SELECT` trên `localization.llm_calls` cho role của downloader | Một migration cấp quyền, có file down; sao lưu `pg_dump` trước khi áp |
| Agent LLM không lộ `activity_started` chi tiết (chạy trong một activity dài) | Chấp nhận độ chi tiết theo activity; không bịa trạng thái "đang nghĩ" giữa chừng |

## Ngoài phạm vi

3D, âm thanh, sửa workflow hoặc agent, ghi chi phí GPU vào DB, SSE (xem phase 3: polling là đủ cho 1-3 người vận hành), ghi nội dung prompt lên giao diện.

## Câu hỏi đã chốt

1. Thêm dependency `@temporalio/client` vào downloader: **đã chấp nhận** (2026-10-31). Phương án ghi event từ worker vào Postgres bị loại vì phải sửa code worker.
2. Nhật ký sự kiện giữ **50 event gần nhất**.

Chưa còn câu hỏi mở. Điểm chưa biết duy nhất là tên activity thật của các agent LLM, được giải bằng spike ở đầu giai đoạn 1.
