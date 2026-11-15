---
phase: 7
title: "Các trang sidebar"
status: pending
priority: P2
effort: "7h"
dependencies: [2, 4]
---

# Phase 7: Các trang sidebar

## Goal

Sidebar có bốn trang thật thay cho các mục của thiết kế (hạng mục 11a đến 11d), mỗi trang chỉ đọc dữ liệu thật: Trình đối soát Media, Node & Cụm Worker, Luồng & Workflow (gồm nhật ký Temporal), Cài đặt chỉ đọc. Nút Tải lại worker (11e) **không làm** (D7).

## Context

- Định tuyến bằng hash (`#/video/<aweme>`) trong `services/web/src/App.tsx`; sidebar ở `services/web/src/components/AppShell.tsx` hiện chỉ có Dashboard và video đang mở.
- `GET /api/dashboard/health` đã trả poller của 7 hàng đợi, `host_worker`, VoiceStudio và đĩa (`dashboard.health.ts:28-38,120-178`), và sau phase 2 cả `host_machine` và `tunnel`. `TaskQueueProbe` (`src/temporal/task-queue-probe.ts`) đọc được `lastAccessTime` nhưng interface chỉ trả số poller, và `buildHealth` cũng dùng interface đó.
- Reader Temporal riêng cho dashboard: `src/workspace/workspace.dashboard-reader.ts` có `listOpenLocalizationIds()` (lọc hai loại workflow, `:35-36`), `workflowStatus(id)` và `inspect`; bộ nhớ đệm lịch sử dùng chung toàn tiến trình và tự xóa sau 200 mục (`:81-89`) — nó cũng phục vụ timeline trang chi tiết video. Namespace còn chạy workflow khác loại (`RevoiceCaptionWorkflow`, `WorkspaceSimulationWorkflow`) mà id không khớp `^video-(\d+)-g(\d+)$`. Test kiến trúc `http-lane-boundary.spec.ts:209-222` chỉ cho `src/workspace` và `src/temporal` import `@temporalio` và cấm start/signal/cancel/terminate.
- Lịch sử Temporal giữ 30 ngày (`docker-compose.yml:311`); visibility dùng Postgres chung.
- **`cueCount` và `mismatch_count` đã có sẵn trong danh sách video** (`dashboard.repository.ts:206`, `services/web/src/api/types.ts:223`): không cần thêm vào response.
- Cấu hình thật chỉ có `DASHBOARD_STUCK_MINUTES` là biến môi trường (`env.ts:170`). TTL link media (`media-token.ts:7`) và trần hàng loạt 100 (`dashboard.schemas.ts:9`) là hằng số trong code; chất lượng render `delivery` là chuỗi cứng trong worker Python (`final_render.py:157`) mà downloader không đọc được. Biến như `DATABASE_URL`, `REDIS_URL` có thể chứa mật khẩu trong URL và không khớp tên kiểu `KEY|SECRET|PASSWORD`.
- `listItem` trong `dashboard.service.ts` dùng chung cho `list()` và `attention()`; thêm trường làm đổi cả hai response (cộng thêm trường tùy chọn thì tương thích).

## Thiết kế

- **11a Trình đối soát Media** (`#/review`): danh sách video đang chờ duyệt, **sắp theo số mismatch giảm dần ở server**: thêm `sort=mismatch_desc` vào `videosQuerySchema` và sắp trước khi cắt trang (danh sách đã nằm trong bộ nhớ, `dashboard.service.ts:209-229`). Cột: mã, kênh, generation, revision, số cue, số mismatch, thời gian chờ; bấm mở trang chi tiết (bảng cue ở phase 4). Dùng `GET /api/dashboard/videos?stage=awaiting_review`; không cần route mới.
- **11b Node & Cụm Worker** (`#/workers`): **không thêm route mới**. Mở rộng `GET /api/dashboard/health` với `host_worker.current` (video đang xử lý, từ `info.current`) và `last_poller_at` cho từng hàng đợi (mở rộng interface `TaskQueueProbe`, cập nhật mọi người dùng của nó). Trang đọc từ health và dùng lại bộ nhớ đệm 5 giây của `DashboardHealthService`, nên chỉ một lần thăm dò Temporal. Hiển thị: hàng đợi và poller, worker review (heartbeat, video đang xử lý), VoiceStudio, thống kê máy host (phase 2).
- **11c Luồng & Workflow + Nhật ký Temporal** (`#/workflows`, D4 phương án a): danh sách workflow đang mở và N workflow vừa kết thúc, mỗi dòng có ID, video, generation, trạng thái và thời gian chạy lấy từ **dữ liệu của lệnh liệt kê** (thời điểm bắt đầu, đóng, trạng thái), không gọi lịch sử cho từng dòng. Route mới `GET /api/dashboard/workflows?state=open|closed&limit=`, chỉ đọc; `listRecentClosed()` trong reader lọc **hai loại workflow** (`VideoLocalizationWorkflow`, `VbeeDubbingWorkflow`) cộng cửa sổ `CloseTime` **7 ngày gần nhất** và trần `limit` **50 dòng** (mặc định; tham số `limit` không vượt 50). <!-- Updated: Validation Session 1 - 7 ngày, tối đa 50 dòng --> Mở một dòng thì tải lịch sử activity theo `aweme_id` + `generation` (kiểm `^\d{1,32}$`; id workflow do **server dựng**, không nhận id từ client), qua một bộ nhớ đệm riêng nhỏ để không đẩy cache của timeline video. Hiển thị lỗi chỉ ở dạng class (`failureType`), không bao giờ thông điệp lỗi (quy tắc sẵn có ở `workspace.temporal.ts:28-29`). Lỗi một phần (liệt kê được, vài dòng không đọc được) hiện `unknown` cho từng dòng. Liên kết sang Temporal UI chỉ hiện ở Localhost (cổng 8233 không qua tunnel).
- **11d Cài đặt** (`#/settings`, chỉ đọc): `GET /api/dashboard/settings` trả danh sách **allowlist cứng** theo đúng từng mục. Mỗi mục có nhãn nguồn: "biến môi trường `X`" (chỉ `DASHBOARD_STUCK_MINUTES`) hoặc "hằng số trong code" (TTL link media, trần hàng loạt, nhịp poll). Không có mục nào mà downloader không biết giá trị thật (bỏ "chất lượng render" trừ khi worker báo trong heartbeat, ngoài phạm vi). **Không bao giờ** trả khóa, mật khẩu, URL kèm userinfo, DSN hay secret; test dùng ảnh chụp snapshot allowlist chính xác cộng kiểm không có giá trị response nào trùng với giá trị secret hoặc URL-có-userinfo đã cấu hình.
- **11e Tải lại worker:** không làm. Khởi động lại worker giữa render hoặc dub làm mất việc đang chạy, và đó là đường điều khiển tiến trình trên máy host từ web. Dùng `pm2 restart` trong terminal; ghi vào `docs/admin-dashboard.md`.

## Files

- Create: `services/downloader/src/dashboard/dashboard.workflows.ts`, `dashboard.settings.ts` (route, schema zod, allowlist) và test
- Modify: `services/downloader/src/workspace/workspace.dashboard-reader.ts` (`listRecentClosed`, cache riêng), `src/temporal/task-queue-probe.ts` (trả `lastAccessTime`) và các nơi dùng interface, `dashboard.health.ts` (`current`, `last_poller_at`), `dashboard.controller.ts`, `dashboard.module.ts`, `dashboard.service.ts` + `dashboard.schemas.ts` (`sort=mismatch_desc`)
- Create: `services/web/src/dashboard/{ReviewQueuePage,WorkersPage,WorkflowsPage,SettingsPage}.tsx` và test cạnh từng file
- Modify: `services/web/src/App.tsx`, `services/web/src/components/AppShell.tsx`, `services/web/src/api/client.ts`, `types.ts`
- Tests: `services/downloader/test/auth/every-route-requires-api-key.e2e-spec.ts` (route mới tự được khám phá), `test/architecture/http-lane-boundary.spec.ts` (không thêm import `@temporalio` ngoài hai thư mục cho phép; lưu ý regex "không gọi ghi" cũng khớp `.execute(`: truy vấn visibility đi qua `list()`/`count()`)

## Tasks

1. Thêm `sort=mismatch_desc`, làm `ReviewQueuePage` trên route danh sách có sẵn.
2. Mở rộng `TaskQueueProbe` và health (`current`, `last_poller_at`); làm `WorkersPage` đọc từ health.
3. Mở rộng reader Temporal với `listRecentClosed` (lọc hai loại workflow, cửa sổ thời gian, trần, cache riêng); route `workflows` và mở rộng dòng theo `aweme_id` + `generation`.
4. Viết route `settings` với allowlist cứng và test snapshot, kiểm không lộ secret.
5. Làm bốn trang UI và cập nhật sidebar (trang đang mở được tô sáng, dùng được ở màn hình hẹp); mục nào chưa có dữ liệu thì ẩn.
6. Test: mỗi route mới trả 401 khi thiếu API key; Temporal sập thì route `workflows` trả 200 với `sources` ghi rõ nguồn lỗi (không giả danh sách rỗng là "không có workflow"); lỗi một phần hiện `unknown` theo dòng; workflow khác loại không xuất hiện; không id workflow nào do client quyết định.

## Verification

- `pnpm --filter downloader test`, `pnpm --filter web lint && test && build` xanh; test kiến trúc vẫn xanh.
- Trên stack thật: mỗi trang hiện dữ liệu khớp Temporal UI và `pm2 list`; tắt `reup-voice-worker` thì trang Workers hiện hàng đợi `stt`/`tts` không có poller.
- Kiểm trình duyệt ở 1440 và 375 px.

## Risks

- `listRecentClosed` có thể nặng nếu lịch sử lớn: trần `limit`, cửa sổ thời gian, không phân trang vô hạn, không lịch sử theo từng dòng.
- Trang Cài đặt dễ lộ nhầm bí mật: allowlist cứng thay vì dump `process.env`, có test snapshot.
- Liên kết Temporal UI qua tunnel không dùng được: chỉ hiện ở Localhost.
