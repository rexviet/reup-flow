---
phase: 3
title: "API đọc cho dashboard"
status: completed
priority: P1
effort: "9h"
dependencies: [1]
---

# Phase 3: API đọc cho dashboard

## Goal

Downloader có module `dashboard` trả về danh sách video kèm giai đoạn, chi tiết một video,
danh sách cần xử lý và sức khỏe hệ thống. Mọi route chỉ đọc; Temporal vẫn chỉ đọc.

## Context

- Mọi route đi qua `ApiKeyGuard` toàn cục (`app.module.ts:61`); chỉ `/health` là public.
- Test kiến trúc `test/architecture/http-lane-boundary.spec.ts:208-222`: chỉ `src/workspace` và `src/temporal` được import `@temporalio`, và không được start/signal/cancel/terminate. Module `dashboard` lấy dữ liệu Temporal **qua provider Nest** export từ `src/workspace`/`src/temporal`, không import `@temporalio`.
- `workspace.temporal.ts`: `listOpen` (183) dừng ở `limit` (workspace gọi với `MAX_OPEN_WORKFLOWS = 10`, `workspace.service.ts:23`), xóa cache history ngoài danh sách (193-195), và lọc `VideoLocalizationWorkflow` + `WorkspaceSimulationWorkflow` (187). Starter còn chạy `VbeeDubbingWorkflow` cùng mẫu ID (`outbox_starter.py:44-47`). Vì vậy dashboard **không** dùng lại `listOpen`. <!-- red-team #7 -->
- Có `find(workflowId)` (`workspace.temporal.ts:199`), `activitiesFromHistory` (97), `pendingFromDescribe` (155). Workflow ID `video-<aweme>-g<gen>`.
- `localization.runs` không có dòng khi run đang chạy; giai đoạn đang chạy phải lấy từ Temporal.
- Run canary (`outbox_starter.py:261-302`) tạo generation mới **không** cập nhật outbox. DB thật: outbox g1, runs tới g3 và g7. <!-- red-team #1 -->
- `ReviewsRepository` (`src/reviews/reviews.repository.ts:13-27`) đã có SQL lọc review project. Dashboard tái dùng SQL này; phase 6 xóa `src/reviews`.

## Generation hiện tại

`current_gen = GREATEST(outbox.generation, MAX(runs.generation), MAX(gen của workflow mở))`.
Mọi giai đoạn, thao tác và media mặc định dùng `current_gen`. Trạng thái outbox chỉ được
dùng khi `outbox.generation = current_gen`. Chi tiết video liệt kê mọi generation. <!-- red-team #1 -->

## Giai đoạn (một hàm thuần, có unit test)

Tính theo thứ tự ưu tiên, trên `current_gen`:

| Giai đoạn | Điều kiện |
|---|---|
| `downloading` / `download_failed` | `videos.status` pending, downloading / failed, skipped |
| `held` | outbox `held` |
| `localizing` + `step` | có workflow mở `video-<aweme>-g<current_gen>`; `step` ∈ `prepare`, `translate`, `dub`, `review` lấy từ activity đang chạy |
| `queued` | outbox `pending`, hoặc `dispatched` mà chưa có workflow mở và chưa có `runs` |
| `dispatch_failed` | outbox `rejected` (input hỏng, chạy lại không giúp được) hoặc `dead` |
| `run_failed` / `skipped` | `runs.status` failed / `skipped_ad`, `skipped_no_video` |
| `awaiting_review` | `runs.status` ready_for_review hoặc needs_human, chưa có quyết định |
| `returned` | `review_decisions.decision = 'returned'` |
| `render_queued` / `rendering` / `render_failed` / `rendered` | theo `final_renders.state`; run legacy `published` có `output_key` coi như `rendered` |
| `published` | `publications.state = 'published'` |

Bảng ánh xạ activity → `step` đặt cạnh `workspace.catalog.ts`. `workspace.catalog.ts` chỉ có
luật regex (`RULES`), không có danh sách activity, nên test dùng một danh sách tên activity
viết tay trong test (lấy từ các `@activity.defn(name=...)` hiện có trong
`services/localization/src`) và khẳng định tên nào cũng có `step`. Danh sách phải gồm 8
activity hiện chưa khớp luật nào: `clip_durations`, `complete_transcript`, `detect_singing`,
`localizer_source_evidence`, `plan_slots`, `review_dub`, `vbee_stt`, `write_slot_script`.
Activity lạ lúc chạy hiện bước con "không rõ" và không vào "Cần xử lý". <!-- Updated: Validation Session 1 - ánh xạ 8 activity, test dùng danh sách viết tay -->

## Cần xử lý

Một video vào hộp "Cần xử lý" khi: `download_failed`, `dispatch_failed`, `run_failed`,
`render_failed`, `needs_human`, `review_status.mismatch_count > 0`, `review_status.dirty`,
activity đang chạy có `attempt > 1` hoặc chạy quá `DASHBOARD_STUCK_MINUTES` (mặc định 30),
**mất dấu** (outbox `dispatched` hơn 10 phút, không có workflow mở, không có `runs`, ví dụ run
bị hủy trong Temporal UI), hoặc **generation cũ đang chờ duyệt** (một generation khác
`current_gen` có `ready_for_review`/`needs_human`, vì `reup-review-auto-open` vẫn mở nó).
Job tải `blocked`/`failed` hiện riêng ở đầu hộp. Mỗi mục kèm lý do tiếng Việt và thao tác gợi ý. <!-- red-team #1 -->

## Routes

- `GET /api/dashboard/videos?stage=&channel=&q=&from=&to=&attention=&limit=&offset=` → `{ items, total, counts_by_stage, sources }`. `q` tìm trong `aweme_id` và `"desc"`. Mọi filter truyền bằng tham số SQL; `stage` theo allowlist; `q` escape `%` và `_`.
- `GET /api/dashboard/videos/:awemeId` → video, kênh, các generation (outbox + runs), timeline activity (tái dùng run detail của workspace), verdict, chi phí LLM, review_status (gồm `revision` để client gửi lại khi duyệt), quyết định, render, phát hành, `dashboard_actions` gần nhất.
- `GET /api/dashboard/attention` → danh sách cần xử lý (dùng chung hàm giai đoạn).
- `GET /api/dashboard/channels` → kênh, công tắc tự localize, job gần nhất, số video theo giai đoạn.
- `GET /api/dashboard/health` → `{ postgres, redis, cookie, temporal, minio, voicestudio, queues: { stt, tts, media, render, llm, io, workflows }, host_worker, disk }`. Mỗi mục có `ok | degraded | down | unknown` và lý do.
- `:awemeId` và mọi `aweme_ids[]` kiểm bằng `/^\d{1,32}$/` như `reviews.schemas.ts:10`.

Nếu Temporal hoặc Postgres sập, route vẫn trả 200 với `sources`, giống snapshot workspace; video đang chạy hiện "không rõ", không hiện "rảnh".

## Đọc Temporal cho dashboard

Thêm vào `src/workspace` một reader riêng, chỉ đọc, không đụng cache của workspace: <!-- red-team #7 -->

- `listOpenLocalizationIds()`: truy vấn `ExecutionStatus="Running" AND WorkflowType IN ("VideoLocalizationWorkflow","VbeeDubbingWorkflow")`, phân trang tới hết, chỉ trả ID và thời điểm bắt đầu.
- `workflowStatus(id)`: dùng `find`/describe đúng một ID, trả `running | closed | not_found`, hoặc ném lỗi khi Temporal không đọc được. Phase 4 dùng hàm này cho `rerun`.

## Sức khỏe

- Queue: thêm `describeTaskQueue` (chỉ đọc) vào `src/temporal`; poller thấy trong 60 giây gần nhất = có worker. `stt`/`tts` đại diện cho `reup-voice-worker`.
- VoiceStudio: đọc `host_workers('review-worker').info.voicestudio` do worker trên máy probe (phase 5). Không gọi thẳng VoiceStudio từ container, vì đường từ container ra host rớt khoảng 45% kết nối (`docker-compose.override.yml:5-8`). <!-- red-team #15 -->
- MinIO: `GET {S3_ENDPOINT}/minio/health/ready`.
- Worker trên máy (`reup-review-worker`) và đĩa: đọc `localization.host_workers`. Quá 2 phút không thấy heartbeat = `down`; khi đó VoiceStudio và đĩa hiện `unknown`.

## Files

- Create: `services/downloader/src/dashboard/{dashboard.module,dashboard.controller,dashboard.service,dashboard.repository,dashboard.stage,dashboard.schemas,dashboard.health}.ts`
- Create: `services/downloader/src/workspace/workspace.steps.ts` (ánh xạ activity → step), `src/workspace/workspace.dashboard-reader.ts`
- Modify: `services/downloader/src/temporal/*` (thêm `describeTaskQueue`), `src/workspace/workspace.module.ts` (export reader), `src/app.module.ts`
- Modify: `services/downloader/src/config/env.ts` và allowlist env của service `nest` trong `docker-compose.yml` (`DASHBOARD_STUCK_MINUTES`) <!-- red-team #14 -->
- Modify: `test/architecture/http-lane-boundary.spec.ts` (thêm `dashboard.module` vào `FEATURE_MODULES`)
- Tests: `services/downloader/test/dashboard/*.spec.ts`; test repository dashboard chạy trên DB migration test có schema `localization` thật (per-worker schema của test helper không có `localization.*`)

## Verification

- Unit test bảng giai đoạn cho từng dòng ở trên, gồm "mất dấu", generation canary mới hơn outbox, và Temporal sập.
- Test reader: hơn 10 workflow mở đều được trả về; workflow Vbee được tính; cache của workspace không bị đụng.
- Test controller: 400 cho query sai, 200 kèm `sources` khi Temporal down.
- Test kiến trúc vẫn xanh: `dashboard` không import `@temporalio`.
- Đo trên dữ liệu thật: `GET /api/dashboard/videos?limit=50` dưới 500 ms; hai video thật hiện g3 và g7.

## Risks

- Sai lệch giữa ba nguồn trong cùng một request (run vừa kết thúc giữa lúc đọc). Ưu tiên dòng `runs` hơn workflow mở.

## Completion notes (2026-11-12)

- Giai đoạn là hàm thuần `dashboard.stage.ts`; `unknown: true` khi Temporal không đọc được và video đã qua bước tải.
- Cảnh báo generation cũ chỉ tính run có `project_key` (đúng điều kiện của `reup-review-auto-open`) và gộp một mục cho mọi generation cũ.
- Activity lạ vẫn vào "Cần xử lý" khi thử lại hoặc chạy quá `DASHBOARD_STUCK_MINUTES`; chỉ nhãn bước con là "không rõ".
- Kiểm trên bản sao dữ liệu thật: hai video hiện g3 và g7, đọc 50 dòng mất khoảng 30 ms; 7 hàng đợi Temporal và MinIO đọc đúng; `host_workers` còn trống nên worker máy báo `down` cho tới phase 5.
- Test repository dùng database tạm có schema `localization` thật (`test/support/localization-db.ts`).
- Chưa kiểm HTTP trên container `nest` vì nó còn chạy code cũ (cần build lại).
