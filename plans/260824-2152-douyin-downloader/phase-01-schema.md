---
phase: 1
title: Schema, migration, claim/reaper query
status: not_started
effort: ~0.5d
blockedBy: [0]
milestone: M1
---

# Phase 1 — Schema + claim/reaper query

Hạt nhân của D5 (DB làm queue). **F1·F3·F5·F6 phải vào cột ngay ở đây** — thêm cột lúc chưa có dữ liệu là miễn phí, thêm sau là migration.

Đọc trước: solution design §6.

---

## 1.1 Ba bảng

`profiles(sec_user_id PK, nickname, last_synced_at)`

`jobs(id uuid PK, sec_user_id → profiles, status, total_videos NULL, enumerated_count, downloaded_count, failed_count, max_cursor bigint, lease_expires_at NULL ← F5, error, created_at, updated_at)`

`videos(aweme_id text PK, sec_user_id, desc, create_time, duration_ms, play_addr NULL ← F1, url_expires_at NULL ← F1, storage_path NULL, status, attempts, last_error, lease_expires_at NULL ← F3)`

`jobs.status`: `pending·enumerating·downloading·completed·failed·blocked·cancelled`
`videos.status`: `pending·downloading·done·failed·skipped`

**`videos` KHÔNG có `job_id` — cố ý (F7).** `aweme_id` là PK toàn cục ⇒ incremental miễn phí, không cần logic gì thêm. Thêm `job_id` sẽ phá dedupe.

## 1.2 Index

| Index | Phục vụ |
|---|---|
| `videos(status, create_time)` partial `WHERE status='pending'` | claim download — index chỉ chứa việc chưa làm |
| `videos(status, lease_expires_at)` partial `WHERE status='downloading'` | reaper |
| `videos(sec_user_id, create_time DESC)` | browse + đếm tiến độ |
| `jobs(status, created_at)` | claim enumerate |
| `jobs(status, lease_expires_at)` partial `WHERE status='enumerating'` | reaper F5 |
| `jobs(sec_user_id)` | |

## 1.3 Claim query — viết ra để không ai đoán

```sql
-- claim 1 video (F6: attempts tăng LÚC CLAIM)
UPDATE videos v SET
  status = 'downloading',
  lease_expires_at = now() + (:lease_seconds || ' seconds')::interval,
  attempts = v.attempts + 1
WHERE v.aweme_id = (
  SELECT aweme_id FROM videos
  WHERE status = 'pending' AND attempts < :max_attempts
  ORDER BY create_time
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;

-- reaper
UPDATE videos SET status='pending', lease_expires_at=NULL
WHERE status='downloading' AND lease_expires_at < now();

UPDATE jobs SET status='pending', lease_expires_at=NULL          -- F5
WHERE status='enumerating' AND lease_expires_at < now();
```

**F6 đánh đổi — ghi vào code comment:** 3 lần crash hạ một video xuống `failed` dù chưa tải thật lần nào. Người vận hành bấm `/retry` để reset. Hướng an toàn hơn: dừng lại thay vì spin vô hạn.

## 1.4 F7 — ghi thành doc, không phải code

Tiến độ job đo theo **trạng thái profile**, không theo delta của job. Job lần 2 trên profile không có video mới hiện **`340/340` ngay lập tức**. Đây là **thông tin đúng** ("mọi thứ đã tải xong"), không phải bug.

Ghi vào `services/downloader/src/db/schema.ts` dưới dạng comment ngay trên bảng `videos`, để người đọc code hiểu vì sao không có `job_id`.

---

## Todo

- [ ] `schema.ts` 3 bảng + comment F7 trên `videos`
- [ ] `migrations/260824-init-schema.sql` forward + rollback (`DROP TABLE`)
- [ ] 6 index (chú ý 3 cái partial)
- [ ] `videos.repository.ts`: `claimOne()` · `release()` · `markDone()` · `markFailed()` · `reapOrphans()`
- [ ] `jobs.repository.ts`: `claimOne()` · `checkpoint()` · `reapOrphans()` (F5)
- [ ] Integration test claim: 2 transaction song song, **không row nào bị claim 2 lần**

## DoD

- [ ] Migration forward + rollback chạy sạch trên DB rỗng
- [ ] Test claim đồng thời N=10: tổng row claim được == số row có, **không trùng**
- [ ] **Mutation:** bỏ `SKIP LOCKED` → test claim đồng thời **đỏ**
- [ ] **Mutation:** chuyển `attempts` sang tăng lúc fail → test crash-loop 3 lần **đỏ**
- [ ] Mỗi mutation **in ra diff nó thật sự áp được** — match rỗng = chưa chạy

## Risks

| Risk | Xử lý |
|---|---|
| Quên partial index → claim quét cả bảng khi có 100k video | DoD kiểm `EXPLAIN` dùng đúng index |
| `attempts < max_attempts` trong subquery bị quên → video failed vẫn bị claim lại | Test: video `attempts=3` không bao giờ được claim |
