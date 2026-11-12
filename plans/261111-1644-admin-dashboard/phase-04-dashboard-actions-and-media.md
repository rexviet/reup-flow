---
phase: 4
title: "Thao tác và media"
status: completed
priority: P1
effort: "8h"
dependencies: [2, 3]
---

# Phase 4: Thao tác và media

## Goal

Dashboard gọi được các thao tác đơn lẻ và hàng loạt, và phát hoặc tải được video gốc, bản
cuối và SRT qua downloader mà không lộ API key.

## Thao tác

Một route: `POST /api/dashboard/actions { action, aweme_ids: string[] (1..100), note?, post_url?, platform?, revision?, confirm_mismatch? }`
→ `{ done: string[], skipped: { aweme_id, reason }[] }`. Mỗi video xử lý trong transaction
riêng với điều kiện trong `WHERE`, nên bấm hai lần hoặc hai tab cùng bấm không gây hỏng.
Mỗi thao tác thành công ghi `dashboard_actions`. Mọi thao tác dùng `current_gen` của phase 3.

| action | Điều kiện | Tác dụng |
|---|---|---|
| `localize` | outbox `held` | phase 2 |
| `rerun` | `workflowStatus("video-<aweme>-g<current_gen>")` là `closed` hoặc `not_found`; outbox `dispatched` hoặc `dead`; render không ở `rendering`. Temporal lỗi → bỏ qua với lý do "Temporal không đọc được". Outbox `rejected` → bỏ qua với lý do "Input hỏng, chạy lại không giúp được" | `UPDATE localization_outbox SET generation = <current_gen + 1>, state='pending', next_attempt_at=now(), dispatch_attempts=0, last_error=NULL, workflow_id=NULL WHERE aweme_id=$1 AND generation=<giá trị đã đọc>` <!-- red-team #1, #7 --> |
| `approve` | `runs.status` ready_for_review/needs_human có `project_key`; `review_status` tồn tại, `dirty` không true, và `review_status.revision = revision` client gửi (bản admin đang xem); nếu `mismatch_count > 0` phải có `confirm_mismatch: true` | upsert `review_decisions(decision='approved', approved_revision=revision)` và `final_renders(state='queued', approved_revision=revision, attempts=0, error=NULL)` trong cùng transaction <!-- red-team #5 --> |
| `return` | đang chờ duyệt hoặc đã render | `review_decisions.decision='returned'`, lưu `note`; xóa dòng `final_renders` chưa `rendering` |
| `rerender` | `final_renders.state` failed hoặc rendered, quyết định vẫn `approved` | `state='queued'`, `attempts=0`, xóa `error` |
| `mark_published` / `mark_unpublished` | đã `rendered` (gồm run legacy có `output_key`) | upsert `publications` |
| `open_studio` | đúng 1 video, có review project | chèn `studio_open_requests(pending)`; worker trên máy mở Studio (phase 5). Chỉ có tác dụng trên máy này |

Cũng đưa vào màn hình này: `retry` và `cancel` của job tải (đã có ở `jobs.controller.ts`).
Không có thao tác hủy run localization (quyết định giữ Temporal chỉ đọc).

## Media

URL media được ký, vì `<video src>` và link tải không gửi được header `X-Api-Key`
(`api-key.guard.ts:39-48`). <!-- red-team #3 -->

- `POST /api/dashboard/media-links { aweme_id, generation, kinds[] }` (cần API key) → URL dạng `/api/dashboard/media/<token>`, token = HMAC-SHA256 trên `(aweme, generation, kind, exp)` với secret `DASHBOARD_MEDIA_SECRET`, sống 10 phút.
- `GET /api/dashboard/media/:token` dùng decorator riêng: guard chấp nhận chữ ký hợp lệ **thay cho** API key chỉ ở route này. Route không bao giờ `@Public()`. Cập nhật `test/auth/every-route-requires-api-key.e2e-spec.ts` để khẳng định route này trả 401 khi không có chữ ký hợp lệ.
- `kind` ∈ `source`, `final`, `srt`. Chuyển tiếp header `Range` để `<video>` tua được. `?download=1` thêm `Content-Disposition: attachment; filename=<aweme>-g<gen>.<ext>`.

`MediaReader` riêng trong `dashboard.media.ts`, không đi qua `Storage` (lớp này không có hàm đọc và luôn gắn `S3_KEY_PREFIX`=`source/`, `s3.storage.ts:304-308`): <!-- red-team #11 -->

- S3 client riêng, chỉ đọc, dùng credential của downloader và **chỉ** bucket cấu hình `S3_BUCKET`; không lấy bucket từ DB.
- Key được dựng lại từ aweme và generation đã kiểm, và chỉ nhận ba dạng: `source/...` (lấy từ `videos.storage_path`, phải cùng bucket), `output/review/<aweme>/g<gen>/...` (từ `final_renders`), `output/<aweme>.mp4` (run legacy, ghi chú "không gắn generation").
- Media yêu cầu `STORAGE_BACKEND=s3`; nếu không thì trả 503 với lý do rõ ràng. Localization vốn từ chối input không phải `s3://` (`outbox_starter.py:180-183`).
- MinIO trả 403 cho object không tồn tại khi không có `ListBucket`: map về 404.

## Quyền MinIO

Identity `downloader` hiện chỉ có `source/*` (`infra/minio/init.sh:38-42`). Thêm
`s3:GetObject` trên `output/*`. Chỉ cập nhật policy `downloader` (`mc admin policy create local downloader <file>`),
**không** chạy lại vòng lặp tạo user của `init.sh`, vì vòng đó đặt lại secret của mọi
user (`init.sh:75-83`). Sửa policy trong `init.sh` để lần dựng stack sau cũng có quyền này,
và sửa comment "source/* only" ở `.github/workflows/ci.yml:250`. <!-- red-team #11 -->

## Files

- Create: `services/downloader/src/dashboard/{dashboard.actions,dashboard.media,media-token}.ts` và tests
- Modify: `services/downloader/src/auth/api-key.guard.ts` (nhận chữ ký media cho đúng route media)
- Modify: `services/downloader/src/config/env.ts`, allowlist env `nest` trong `docker-compose.yml`, `.env.example` (`DASHBOARD_MEDIA_SECRET`)
- Modify: `infra/minio/init.sh`, `.github/workflows/ci.yml` (comment)
- Modify: `services/downloader/test/auth/every-route-requires-api-key.e2e-spec.ts`

## Verification

- Test từng action: đủ điều kiện → `done`; sai điều kiện → `skipped` kèm lý do; chạy hai lần song song chỉ một lần có hiệu lực.
- Test `rerun`: từ chối khi workflow đang chạy, khi Temporal lỗi, khi outbox `rejected`, khi `generation` đã đổi; sau canary g7 thì tạo g8.
- Test `approve`: từ chối khi revision client gửi khác `review_status.revision`, khi `dirty`, khi có mismatch mà thiếu `confirm_mismatch`.
- Test media: không có chữ ký → 401; chữ ký hết hạn → 401; `Range: bytes=0-99` → 206 với 100 byte; key thiếu → 404; `kind` lạ → 400.
- Trên stack local: downloader đọc được `output/`, vẫn bị từ chối ở `review/`; `mc admin policy entities` xác nhận policy gắn đúng user; các identity khác vẫn hoạt động.

## Risks

- Proxy video lớn qua Nest: dùng stream, không buffer cả file.

## Implementation notes

- `approve` takes exactly one video (a single `revision` cannot match several videos); `open_studio` likewise. All other actions are bulk up to 100.
- Optional `generation` in the body pins an action to what the admin was viewing; a mismatch is skipped as "trạng thái vừa đổi".
- `retry`/`cancel` of download jobs stay on the existing `/api/jobs/:id/retry|cancel` routes; the web screen (phase 6) calls them directly.
- Local stack: `downloader` policy updated live (reads `output/*`, denied `review/*` and writes to `output/`). The running `nest` container still has the old code until rebuilt; set `DASHBOARD_MEDIA_SECRET` (32+ chars) in `.env` before using media.

## Implementation notes

- `approve` takes exactly one video (a single `revision` cannot match several videos); `open_studio` likewise. All other actions are bulk up to 100.
- Optional `generation` in the body pins an action to what the admin was viewing; a mismatch is skipped as "trạng thái vừa đổi".
- `retry`/`cancel` of download jobs stay on the existing `/api/jobs/:id/retry|cancel` routes; the web screen (phase 6) calls them directly.
- Local stack: `downloader` policy updated live (reads `output/*`, denied `review/*` and writes to `output/`). The running `nest` container still has the old code until rebuilt; set `DASHBOARD_MEDIA_SECRET` (32+ chars) in `.env` before using media.
