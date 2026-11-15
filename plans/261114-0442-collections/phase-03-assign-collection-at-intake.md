---
title: "Phase 3: Gán collection lúc tải"
status: done
priority: P1
effort: "1d"
dependencies: [1, 2]
---

# Phase 3: Gán collection lúc tải

## Overview

Mọi đường ghi video (enumerate kênh và thêm video lẻ) gán `collection_id` và `mix_episode` theo quy
tắc: collection chọn tay, rồi collection 合集, rồi collection mặc định của kênh. Video đã có trong DB
được chuyển sang 合集 khi tải lại, trừ khi người dùng đã chọn hoặc chuyển tay.

## Key Insights

- Enumerate (`lanes/enumerate.lane.ts:314-320`) gọi `upsertMany(this.db, …)` rồi `checkpoint`, không
  có transaction. Không thêm transaction: việc tạo collection là các lệnh idempotent chạy trước
  `upsertMany`. <!-- red-team: transaction -->
- `upsertMany` là `ON CONFLICT DO NOTHING` (`db/videos.repository.ts:161`), nên video cũ cần một
  UPDATE riêng để vào 合集. <!-- quyết định: video cũ -->
- Video lẻ: `videos/videos.service.ts:44-64`. Job kênh: `jobs/jobs.service.ts:41`; job được claim ở
  `jobsRepository.claimOne`, nên dòng job claim về phải mang `collection_id`.

## Requirements

- Một hàm `resolveCollections(exec, { secUserId, channelName, manualCollectionId, items })` trả về
  `collection_id` cho từng item; enumerate và video lẻ cùng dùng.
- Tạo collection: `INSERT … ON CONFLICT (kind, external_id) DO NOTHING` rồi `SELECT`.
- Tên: collection 合集 lấy `mixName` đã làm sạch; collection kênh lấy nickname từ item. Ghi
  `profiles.nickname` khi đang NULL. Tự đổi tên collection kênh hoặc 合集 chỉ khi `name` vẫn bằng
  `external_id` (người dùng chưa đổi tên).
- Lựa chọn tay (form tải) đặt `collection_manual = true`. Collection chọn tay phải tồn tại, nếu không
  API trả 400 trước khi tạo job hoặc tải video.
- Video đã có: sau `upsertMany`, `UPDATE videos SET collection_id = <mix>, mix_episode = <tập>` với
  điều kiện `collection_manual = false` và `collection_id` đang là collection kênh mặc định. Video thuộc
  合集 mà chưa có số tập thì điền `mix_episode`.

## Related Code Files

- Create: `services/downloader/src/db/collections.repository.ts`
- Modify: `services/downloader/src/db/videos.repository.ts` (`VideoUpsert` có `collectionId`,
  `mixEpisode`, `collectionManual`; hàm chuyển video cũ sang 合集)
- Modify: `services/downloader/src/lanes/enumerate.lane.ts`
- Modify: `services/downloader/src/videos/videos.service.ts`, `videos/videos.schemas.ts`,
  `videos/video-enqueue.controller.ts`
- Modify: `services/downloader/src/jobs/jobs.service.ts`, `jobs/jobs.schemas.ts`, `jobs/jobs.controller.ts`,
  `db/jobs.repository.ts` (`upsertProfile`, `claimOne`)

## Implementation Steps

1. Test trước (Postgres test):
   - Trang enumerate từ fixture phase 1: 10 video vào collection `mix` tên `成名的代价` với
     `mix_episode` 1–10; video còn lại vào collection `channel` tên `IM-薛珍珠`.
   - Video đã có sẵn trong collection kênh (giống dữ liệu sau backfill): enumerate lại thì chuyển sang
     合集 với đúng số tập; video có `collection_manual = true` thì không đổi.
   - Chạy enumerate hai lần: số collection không đổi.
   - Job có `collection_id` tay: mọi video mới vào collection đó với `collection_manual = true`.
   - Video lẻ có `mix_info` vào 合集; không có thì vào collection kênh.
   - Hai lane cùng tạo collection cho một `mix_id`: chỉ có một dòng. Test phải chứng minh hai lệnh
     thật sự chồng nhau (ví dụ chặn bằng advisory lock rồi thả), không chỉ dùng `Promise.all`.
   - Collection kênh tên `sec_user_id` được đổi theo nickname; collection người dùng đã đổi tên thì giữ.
   - `collection_id` tay không tồn tại: 400, không tạo job.
2. Viết repository và `resolveCollections`.
3. Nối vào enumerate, video lẻ, job kênh, và hai schema request.

## Todo

- [x] Test các trường hợp trên
- [x] Repository, resolve, chuyển video cũ sang 合集
- [x] Nối vào ba đường tải và hai schema request

## Ghi chú triển khai

- `services/downloader/src/db/collections.repository.ts`: `resolveCollections` (chọn tay → 合集 → kênh;
  luôn tạo collection kênh và collection 合集), `intakeVideos` (resolve → `upsertMany` → `moveIntoMixes`),
  `collectionExists`, `CollectionNotFoundError`. Enumerate và video lẻ cùng đi qua `intakeVideos`.
- `VideoUpsert` (kết quả của mapper phase 1) giữ nguyên; `upsertMany` nhận `VideoUpsert & CollectionPlacement`
  và không còn tự tạo collection. Ghi `profiles.nickname` nằm trong `resolveCollections`, nên `upsertProfile`
  không đổi. `claimOne` vốn trả cả dòng, nên job claim về đã mang `collection_id`.
- Collection chọn tay của job chỉ áp cho video mới; video cũ chưa chọn tay vẫn được chuyển vào 合集 (đúng
  `plan.md`). Ở đường video lẻ, `collection_id` của form cũng áp cho video đã có (chuyển và đánh dấu tay).
  Video xếp tay vẫn giữ `mix_episode`.
- `collection_id` sai định dạng hoặc không tồn tại: 400 trước khi gọi parser, không tạo job, không ghi video.
- Kiểm đột biến: 34 đột biến, 33 bị test hành vi giết, 1 đột biến tương đương. Test đồng thời xác nhận
  writer B ở `wait_event_type = 'Lock'` trong lúc transaction của A còn mở.
- Việc để lại: phase 4 map lỗi FK khi có đường xóa collection, chốt tiền tố `E01-` cho video xếp tay; phase 7
  bỏ khóa `collection_id` khi không chọn (null bị 400). Xem
  [review](../reports/code-reviewer-261114-1610-collections-phase-03.md).

## Success Criteria

- `pnpm --filter @reup-flow/downloader test` xanh; mỗi test đỏ khi bỏ nhánh tương ứng.

## Risk Assessment

- Lỗi giữa lúc tạo collection và `upsertMany` để lại collection rỗng; lần chạy lại dùng tiếp nó, nên
  không sinh trùng.
