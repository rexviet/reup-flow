---
phase: 1
title: Schema, migration, claim/reaper query
status: completed
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

- [x] `schema.ts` 3 bảng + comment F7 trên `videos`
- [x] `migrations/260824-init-schema.sql` forward + `260824-init-schema.down.sql` rollback (`DROP TABLE`)
- [x] 6 index (chú ý 3 cái partial)
- [x] `videos.repository.ts`: `claimOne()` · `release()` · `markDone()` · `markFailed()` · `reapOrphans()`
- [x] `jobs.repository.ts`: `claimOne()` · `checkpoint()` · `reapOrphans()` (F5)
- [x] Integration test claim: 12 transaction song song trên 10 row, **không row nào bị claim 2 lần**

## DoD

- [x] Migration forward + rollback chạy sạch trên DB rỗng (+ re-runnable: rollback rồi forward lại)
- [x] Test claim đồng thời N=10: tổng row claim được == số row có, **không trùng**
- [x] **Mutation:** bỏ `SKIP LOCKED` → test claim đồng thời **đỏ** (cả `videos` lẫn `jobs`)
- [x] **Mutation:** chuyển `attempts` sang tăng lúc fail → test crash-loop 3 lần **đỏ**
- [x] Mỗi mutation **in ra diff nó thật sự áp được** — `test/mutations/phase-01.sh` coi match rỗng là **lỗi cứng**, không phải "pass"

## Risks

| Risk | Xử lý |
|---|---|
| Quên partial index → claim quét cả bảng khi có 100k video | DoD kiểm `EXPLAIN` dùng đúng index |
| `attempts < max_attempts` trong subquery bị quên → video failed vẫn bị claim lại | Test: video `attempts=3` không bao giờ được claim |

---

## Outcome

Commit dưới đây. **74 test xanh** (thêm 38 test phase 1), **8 mutation** đều áp được và đều bắt được.

`test/mutations/phase-01.sh` là artifact chạy được, không phải quy trình tay: mỗi mutation in diff nó áp, và **match rỗng là lỗi cứng** — đúng cái bẫy đã sập hai lần. CI gọi script này.

**Bốn thứ lệch khỏi bản phác trong §1.3, có chủ ý:**

1. **Reaper áp luật cạn attempt, không phải `→ pending` vô điều kiện.** SQL phác ở §1.3 luôn trả video về `pending`; nhưng claim query lọc `attempts < max_attempts`, nên một row cạn attempt sẽ nằm `pending` **vĩnh viễn mà không ai claim được**, trong khi mọi truy vấn tiến độ vẫn đếm nó là việc chưa xong. Bản implement dùng `CASE WHEN attempts >= max THEN 'failed' ELSE 'pending' END` ở **cả ba** đường trả việc (`release` · `markFailed` · `reapOrphans`) — đây chính là câu văn §1.3 đã viết ("3 lần crash hạ một video xuống `failed`"), bản SQL phác mới là bản rút gọn. Có test `never leaves a video pending but unclaimable`.
2. **`jobs.checkpoint()` chỉ tiến, không lùi.** Lane bị reap có thể vẫn đang bay khi lane thay thế đã paging qua nó; checkpoint muộn của thằng cũ sẽ kéo `max_cursor` lùi lại và bắt lane mới enumerate lại trang đã có. Dùng `greatest()` cho cả `max_cursor` lẫn `enumerated_count` — an toàn mà **không phải thêm cột ownership token**. `checkpoint()` trả `boolean` "job còn là của mình không" để phase 5 biết lúc nào phải dừng.
3. **Migration tách hai file** — `260824-init-schema.sql` + `260824-init-schema.down.sql`. Rollback cố tình **không** `CASCADE`: một object phụ thuộc ngoài dự kiến phải làm nó đỏ, không phải bị kéo theo im lặng.
4. **`markDone()` cố ý KHÔNG kiểm lease.** Race "worker bị reap vẫn ghi xong" đóng ở **phase 6** tại đúng chỗ đóng được: `.tmp` đặt tên duy nhất theo lượt claim + kiểm lease ngay trước `rename`. Kiểm lease ở đây trông như bảo vệ nhưng không bảo vệ gì — lease còn sống không nói lên nó là lease của ai. Đã ghi thành comment tại chỗ.

**Thêm ngoài DoD, vì DoD một mình chưa đủ:**

- **Test chống drift schema↔migration.** Hai artifact tả cùng ba bảng, không có gì trong type system buộc chúng khớp; lệch nhau thì lỗi hiện ra lúc chạy ở lane lúc 3 giờ sáng chứ không phải lúc build. `schema-migration.spec.ts` áp **đúng file migration thật** rồi so catalog với định nghĩa Drizzle **hai chiều** (thừa cột ở bên nào cũng đỏ), so tên index, so **hình dạng** index (đổi thứ tự cột vẫn giữ nguyên tên, vẫn partial, planner vẫn dùng — nên tên không đủ), và so predicate của 3 index partial.
- **F7 biến từ doc thành guard.** Doc mục ruỗng; test khẳng định `videos` **không bao giờ** có `job_id` **và** lời giải thích vẫn nằm ngay trên bảng, chỗ người đọc tiếp theo đập vào mắt trước khi họ với tay sửa.
- **Test `EXPLAIN`** trên 50k video + 50k job, phân bố thật (phần lớn `done`, một lát `pending`, vài cái đang bay) + `ANALYZE`. Câu SQL đem đi EXPLAIN được **bắt từ chính lời gọi repository** qua logger của Drizzle, không gõ lại — câu gõ lại sẽ trôi khỏi repository rồi báo cáo vui vẻ về một plan không ai chạy. Bốn test này **xanh ngay lần chạy đầu**, tức là chưa từng đỏ, nên đã xoá từng index để chứng minh cả bốn đều đỏ được.
- **Mỗi suite db chạy trên schema Postgres riêng theo jest worker.** Jest chạy song song các file test và cả ba file đều thao tác trên cùng tên bảng; dùng chung một schema thì một test claim ở file A sẽ đua với một truncate ở file B — flaky trông **y hệt** bug concurrency thật, và dạy ta quen bỏ qua nó. Tách schema giữ được song song, đồng thời đẩy DDL ra khỏi `public`.
- **`test/support/concurrency-harness.ts`** — barrier chồng lấp + giữ khoá của phase 0 nâng lên thành hạ tầng dùng chung, generic theo kiểu executor. Suite claim phase 1 dùng lại **nguyên** nó: bài học FG-1 (`Promise.all` không phải concurrency, và `LockRows` tự chữa lành một `SKIP LOCKED` thiếu khi commit nhanh) áp cho `videos` y như cho `spike_items`.
- **Guard DDL** (`test/support/pg-target-guard.ts`) nâng từ spike lên: phase 0 chỉ drop `spike_items` của chính nó, phase 1 drop **bảng thật**. `TEST_DATABASE_URL` do người vận hành đặt và mặc định trỏ vào đúng database `reup` mà compose đang chạy.
