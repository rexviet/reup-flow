---
phase: 6
title: "Giao diện dashboard"
status: completed
priority: P1
effort: "13h"
dependencies: [3, 4]
---

# Phase 6: Giao diện dashboard

## Goal

`services/web` chỉ còn một trang dashboard: thanh sức khỏe, hộp "Cần xử lý", bảng pipeline
có lọc và chọn nhiều, trang chi tiết video và khu vực kênh/job tải. Gỡ tab Workspace và
trang Review. Web gọi API bằng đường dẫn tương đối và mọi cổng chỉ bind vào loopback.

## Context

- React 18.3, Vite 6, Tailwind 3.4, Vitest; routing bằng hash ở `App.tsx:17-24`, không có router.
- `api/client.ts:21-22` gọi `VITE_API_BASE_URL`, mặc định `http://localhost:3300` (`docker-compose.yml:210-217`); CORS chỉ cho `http://localhost:5173` (`.env.example:15`). Qua tunnel, cách này hỏng. nginx của web đã proxy `/api` và `/internal` (`services/web/nginx.conf:5-8`). <!-- red-team #4 -->
- Cổng host đang mở trên mọi interface: `3300:3000` (`docker-compose.yml:96`), `5433:5432` (`:159`), `6379:6379` (`:191`), `5173:80` (`:220`). <!-- red-team #4 -->
- `usePolling` giữ dữ liệu cũ khi lỗi. Component tái dùng: `JobForm`, `JobCard`, `ProgressBars`, `CookieBadge`.

## Màn hình

- **Thanh trên cùng:** badge sức khỏe từ `/api/dashboard/health` (poll 10 giây), gồm Cookie Douyin, VoiceStudio, voice worker (`stt`/`tts`), worker review trên máy, Temporal, MinIO, đĩa. Mỗi badge có tooltip lý do.
- **Cần xử lý:** danh sách gọn từ `/api/dashboard/attention`, mỗi dòng có lý do và nút thao tác gợi ý.
- **Pipeline:** hàng chip đếm theo giai đoạn (bấm để lọc), ô tìm kiếm, lọc kênh và ngày. Bảng có checkbox chọn nhiều, cột: video (aweme, mô tả, kênh), generation, giai đoạn (kèm bước con khi đang localize), thời gian ở giai đoạn này, cập nhật lần cuối. Thanh thao tác hàng loạt hiện khi có chọn: Đưa vào localization, Chạy lại, Duyệt, Render lại, Đánh dấu đã đăng. Kết quả hiện số thành công và lý do bị bỏ qua. Poll 3 giây, tạm dừng khi tab ẩn.
- **Chi tiết video** (`#/video/<aweme>`): video gốc và bản cuối cạnh nhau (URL ký lấy từ `POST /api/dashboard/media-links`, làm mới trước khi hết hạn); timeline các bước; verdict của Reviewer và số mismatch; chi phí LLM; các generation; nút Duyệt (gửi kèm `review_status.revision` đang hiển thị) / Trả về (kèm ghi chú), Render lại, tải MP4/SRT, đánh dấu đã đăng (nhập link); nút **Mở Studio** (tạo yêu cầu, hiện trạng thái xử lý; ghi chú "Studio mở trên máy chạy dashboard"); lịch sử thao tác. <!-- red-team #3, #5 -->
- **Kênh và tải:** form tải link hoặc kênh (`JobForm`) có công tắc "Tự localize" (mặc định tắt) và ô "Localize ngay" cho link video đơn; danh sách job với Thử lại/Hủy; danh sách kênh với công tắc tự localize.

Thao tác có thể tốn tiền hoặc thời gian máy (Chạy lại, Duyệt hàng loạt) phải có hộp xác nhận
ghi rõ số video. Duyệt khi có mismatch phải xác nhận riêng.

## Gọi API và cổng

- Build web với `VITE_API_BASE_URL=""` để mọi request đi qua `/api` cùng origin, qua nginx. <!-- red-team #4 -->
- `docker-compose.yml`: bind `127.0.0.1:5173:80`, `127.0.0.1:3300:3000`, `127.0.0.1:5433:5432`, `127.0.0.1:6379:6379`. Extension và script trên máy vẫn dùng `localhost:3300`.
- Giữ CORS như cũ (request cùng origin không cần CORS).

## Gỡ bỏ

<!-- red-team #14 -->
- Xóa `services/web/src/workspace/` (gồm test), `src/reviews/ReviewProjectsPage.tsx` và test, các hàm và type `getWorkspaceSnapshot`, `getRunDetail`, `listReviews` trong `api/`.
- Sửa `services/web/src/App.test.tsx` (đang mock `getWorkspaceSnapshot`, `listReviews` và bấm các tab cũ, dòng 100-122, 155).
- Xóa `services/downloader/test/workspace/web-layout.spec.ts`. Giữ nguyên backend `/api/workspace/*` và các test còn lại.
- Xóa `services/downloader/src/reviews/` và `test/reviews/*`; bỏ import ở `src/app.module.ts:16,58`.
- Sửa `test/auth/every-route-requires-api-key.e2e-spec.ts:119`: thay `/api/reviews` bằng `/api/dashboard/videos`.
- Sửa `test/architecture/http-lane-boundary.spec.ts:231` (đang `require` `reviews.module`).

## Files

- Modify: `services/web/src/App.tsx`, `App.test.tsx`, `src/api/client.ts`, `src/api/types.ts`, `src/components/JobForm.tsx`
- Modify: `docker-compose.yml` (bind cổng, build arg của web)
- Create: `services/web/src/dashboard/{DashboardPage,HealthBar,AttentionList,PipelineTable,BulkActionBar,VideoDetail,ChannelsPanel}.tsx`, `useDashboard*.ts` hooks và test cạnh từng file
- Delete: như mục Gỡ bỏ

## Verification

- Vitest: lọc theo chip, chọn nhiều rồi bấm thao tác gọi đúng API với đúng `aweme_ids`, hiện lý do bị bỏ qua; badge đổi màu theo trạng thái; trang chi tiết hiện đúng nút theo giai đoạn; Duyệt gửi đúng revision.
- `pnpm --filter web lint && pnpm --filter web test && pnpm --filter web build` xanh; `pnpm --filter downloader test` xanh sau khi gỡ `src/reviews`.
- `curl http://<IP LAN của máy>:3300/health` bị từ chối kết nối; `curl http://127.0.0.1:3300/health` vẫn trả lời.
- Kiểm thử trên trình duyệt (skill `/qa` hoặc `/browse`) với stack thật: các luồng ở phase 7, ở cả màn hình hẹp; video tua được.

## Risks

- `VITE_API_KEY` vẫn nằm trong bundle. Rủi ro được chấp nhận khi mọi cổng chỉ bind loopback và tunnel có Cloudflare Access (phase 7).
- Poll nhiều route cùng lúc: gộp `counts_by_stage` vào response danh sách để không cần route riêng.

## Kết quả

- Hoàn thành 2026-11-12. Web: 77 test, lint, tsc, build xanh. Downloader: 563 test của dashboard, auth, env, architecture, workspace, jobs, videos xanh trên Postgres tạm.
- Thêm vào API danh sách (cần cho bảng): `stage_since`, `updated_at`, `revision`, `mismatch_count`; chi tiết thêm `studio_request` (trạng thái yêu cầu mở Studio mới nhất).
- Duyệt: nút ở thanh hàng loạt chỉ bật khi chọn đúng một video, hộp xác nhận ghi revision và số mismatch; trang chi tiết gửi `review_status.revision` đang hiện.
- Chưa làm: kiểm tra trên trình duyệt với stack thật và lệnh `curl` vào IP LAN (container `nest` và `web` còn bản cũ, cần build lại); thuộc phase 7.
