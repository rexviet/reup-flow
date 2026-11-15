---
phase: 4
title: "Bảng cue và chuyển generation"
status: pending
priority: P1
effort: "10h"
dependencies: [3]
---

# Phase 4: Bảng cue và chuyển generation

## Goal

Trang chi tiết video hiện bảng cue phụ đề đối soát (lời dịch, lời đọc, thời gian, cue lệch) cho generation đang chọn, và có bộ chuyển generation đổi cả cue lẫn media (hạng mục 2).

## Context

- `ReviewCue` (`services/localization/src/localization/contracts/review_project.py:28`): `id`, `source_segment_ids`, `start_ms`, `end_ms`, `text_vi`, `status` (`translated | inferred | unresolved`), `reason`, `clip`, `spoken_text`, `audio_start_ms`, `audio_duration_ms`, `stretch_ratio`, `overflow_unresolved`. Cue nằm trực tiếp trong manifest của revision, nên quét lấy chúng không tốn thêm đọc nào (`review_scan.py:29-37` đã nạp `project.cues`).
- `mismatch(cue)` ở `review_bridge/models.py:33` quyết định cue lệch.
- Tài sản của project nằm dưới `review/<aweme>/g<gen>/assets/` (`review_project.py:84`), manifest của revision dưới `review/<aweme>/g<gen>/r<rev>/manifest.json`.
- Identity MinIO `downloader` không đọc được `review/*` (`infra/minio/init.sh:42-44`), nên downloader không tự đọc manifest.
- Worker review đã quét mỗi 60 giây và ghi `localization.review_status` (`revision`, `cue_count`, `mismatch_count`, `dirty`) bằng một UPSERT autocommit cho mỗi project (`review_scan.py:18-26,41-52`, `main.py:31-34`). Vòng quét **chỉ chọn** run `ready_for_review` hoặc `needs_human` (`review_scan.py:10-16`), nên generation đã duyệt, đã render, hoặc có từ trước khi triển khai không có dòng.
- Nút Duyệt so `revision` client gửi với `review_status.revision` và cờ `dirty` (`dashboard.actions.ts:230-260`); cue hiển thị phải cùng revision đó.
- `cueCount` đã có sẵn trong danh sách video (`dashboard.repository.ts:206`, `services/web/src/api/types.ts:223`).
- `generations[]` đã có trong chi tiết video; `POST /api/dashboard/media-links` và các thao tác đã nhận `generation`.
- Downloader chạy bằng superuser `reup`, đọc được `localization.*`.

## Thiết kế

- **Cue trên chính `review_status`:** thêm cột `cues jsonb` vào `localization.review_status` thay vì một bảng song song (cùng khóa, cùng người ghi, cùng revision). Worker ghi `cues` trong **cùng câu UPSERT** với `revision`, `cue_count`, `mismatch_count` để hai thứ luôn cùng revision; chỉ ghi lại `cues` khi `revision` đổi (`CASE WHEN review_status.revision IS DISTINCT FROM EXCLUDED.revision THEN EXCLUDED.cues ELSE review_status.cues END`).
- **Trường cue:** chỉ giữ `id`, `start_ms`, `end_ms`, `text_vi`, `spoken_text`, `status`, `reason`, cờ `mismatch` (tính bằng đúng hàm `mismatch`, không viết lại logic ở TypeScript), `overflow_unresolved`. Nếu tìm được văn bản gốc từ `source_segment_ids` (xem "Chưa xác minh" ở `plan.md`) thì thêm `source_text`; không thì bỏ cột.
- **Backfill:** một lượt chạy một lần khi worker khởi động, ghi `cues` cho mọi run có `project_key` mà `review_status.cues` còn `NULL` (kể cả generation đã duyệt hoặc đã render). Vòng quét thường vẫn chỉ quét run đang chờ duyệt.
- **API:** `GET /api/dashboard/videos/:awemeId/cues?generation=` trả `{ generation, revision, checked_at, cues[] }`; `generation` mặc định là generation hiện tại, kiểm tra thuộc `generations[]`; `awemeId` kiểm `^\d{1,32}$`. Chưa có project hoặc chưa có cue thì trả `404` với lý do tiếng Việt rõ ràng, không trả mảng rỗng giả. Không sửa cue từ dashboard.
- **UI:** bảng cue trên trang chi tiết, cue lệch được tô kèm lý do; bộ chuyển generation (thay danh sách dạng chữ) đổi cue và link media; hiện `revision` đang xem và đánh dấu "bản đã duyệt" khi khớp quyết định. Trạng thái "Chưa có dữ liệu cue cho generation này" khi 404. Nếu generation chọn khác generation hiện tại, nút thao tác bị tắt kèm giải thích "đang xem generation cũ".
- **Phạm vi cue:** chỉ revision HEAD mà quét thấy lần cuối; cue của một revision đã duyệt mà HEAD đã đi tiếp không được lưu. UI nói rõ revision đang hiển thị. <!-- Updated: Validation Session 1 - đã xác nhận chỉ lưu HEAD -->

## Files

- Create: `services/localization/migrations/<ngày>-review-status-cues.sql` và `.down.sql` (thêm cột `cues jsonb`); đăng ký theo bảng ở `plan.md` mục "Đăng ký migration"
- Modify: `services/localization/src/localization/review_worker/review_scan.py` (UPSERT có `cues`, backfill một lần), `main.py` (gọi backfill khi khởi động)
- Create: `services/downloader/src/dashboard/dashboard.cues.ts` (route, truy vấn, schema zod)
- Modify: `services/downloader/src/dashboard/dashboard.controller.ts`, `dashboard.repository.ts`, `dashboard.schemas.ts`
- Create: `services/web/src/dashboard/CueTable.tsx`, `GenerationSwitcher.tsx`; Modify `VideoDetail.tsx`, `services/web/src/api/client.ts`, `types.ts`
- Tests: `services/localization/tests/review_worker/`, `services/downloader/test/dashboard/`, các test web cạnh component

## Tasks

1. Sao lưu DB, thêm migration cột `cues` (bảng đã thuộc `localization`, không cần đổi owner), kiểm down/up.
2. Worker: gom `cues` vào UPSERT hiện có (một câu lệnh, nguyên tử), kèm điều kiện chỉ ghi khi revision đổi; thêm lượt backfill khi khởi động.
3. Tìm nguồn văn bản gốc của cue; quyết định có cột `source_text` hay không và ghi vào journal của phase.
4. Route `cues` với kiểm đầu vào; chưa có cue thì 404.
5. UI: `CueTable` (cột theo dữ liệu có), `GenerationSwitcher`, trạng thái rỗng và lỗi.
6. Test: cue lệch khớp hàm `mismatch`; `cues` và `revision` luôn cùng giá trị sau một lần ghi; generation lạ trả 400; chưa có cue trả 404; đổi generation đổi link media; nút Duyệt gửi đúng revision đang hiển thị và tắt khi xem generation cũ; backfill điền được generation đã render.

## Verification

- Test Python, downloader và web xanh; route mới tự nằm trong test yêu cầu API key (`every-route-requires-api-key` tự khám phá route).
- Trên stack thật: mở video đang chờ duyệt, bảng cue khớp `mismatch_count` ở hộp "Cần xử lý"; mở một video đã render thì thấy cue sau backfill.
- Kích thước: đo `cues jsonb` của một video dài (vài trăm cue); nếu quá lớn thì chỉ lưu cue lệch kèm tổng số.

## Risks

- Văn bản gốc có thể không nằm gần cue: chỉ hiện cột nếu tìm thấy, không bịa.
- Cue trong DB cũ hơn Studio tối đa 60 giây: UI hiện `checked_at` và `revision`, Duyệt vẫn kiểm revision ở server.
- Backfill đọc nhiều manifest một lần ở lần khởi động đầu: chạy theo lô nhỏ, trên luồng của lượt quét, có trần số project mỗi vòng.
