---
phase: 5
title: Enumerate lane — global 1 luồng
status: not_started
effort: ~0.5d
blockedBy: [4]
milestone: M1
---

# Phase 5 — Enumerate lane

Đọc trước: solution design §5.2 (sequence) · `plan.md` §2 (F2) · IDEA.md §3.1 §6.2.

---

## 5.1 F2 — concurrency = 1 **toàn hệ thống**, không phải 1 mỗi job

**Rate limit của Douyin theo IP, không theo job.** 3 người bấm 3 job = 3× tần suất request = ban. Job thứ 2 **xếp hàng chờ**.

Hiện thực: **đúng một** instance `EnumerateLane` được đăng ký. Không phải semaphore, không phải config — là số lượng loop. Cấu trúc bảo đảm, đây là lý do chọn A1.

## 5.2 Vòng lặp

```
claim 1 job (SKIP LOCKED) + lease 10m
cursor = job.max_cursor ?? 0
loop:
  page = parser.fetchUserPostPage(sec_user_id, cursor, count=20)

  if page.awemeList rỗng && page.hasMore == 1:
      → BLOCKED (xem 5.3), giữ nguyên checkpoint, dừng

  upsert videos ON CONFLICT (aweme_id) DO NOTHING
        kèm play_addr + url_expires_at = now + URL_TTL_SECONDS   ← F1
  job.enumerated_count += page.awemeList.length
  job.max_cursor = page.maxCursor          ← CHECKPOINT MỖI TRANG
  heartbeat lease
  if !page.hasMore: break
  sleep(3–5s + jitter)

job.total_videos = count(videos WHERE sec_user_id)
job.status = 'downloading', nhả lease
```

**Checkpoint mỗi trang, không phải cuối vòng lặp.** Crash giữa job thì resume từ trang cuối đã lưu.

**`sleep` nằm TRONG vòng lặp, giữa các trang.** Đây chính là yêu cầu mà `@Interval` không đặt đúng tầng được — lý do #1 chọn A1 (solution design §4).

**Lưu `play_addr` ngay lúc enumerate (F1)** là toàn bộ chỗ tiết kiệm: profile 500 video tốn ~25 request thay vì 525.

## 5.3 Phát hiện `blocked` — phase này CHỈ dùng tín hiệu cấu trúc

`awemeList` rỗng **nhưng** `hasMore == 1` → cookie bị chặn. Đây là HTTP **200** và là chẩn đoán **chắc chắn** — dùng ngay, không cần probe.

→ `status = 'blocked'`, **giữ nguyên `max_cursor`**, dừng. **Không phải `failed`.**

**Xử lý HTTP 400 để sang phase 7.** Ở phase 5, 3× 400 liên tiếp → dừng job ở `blocked` **tạm thời** kèm comment `TODO(phase-7)`. Lý do không đoán bây giờ: parser bóp mọi lỗi thành 400 vô nghĩa (F4), nên đoán ở đây sẽ báo `blocked` cho profile private và người vận hành đi đổi cookie vô ích.

---

## Todo

- [ ] `enumerate.lane.ts`, **đúng 1 instance**
- [ ] Claim job + lease 10m + heartbeat mỗi trang
- [ ] Upsert `ON CONFLICT DO NOTHING` kèm `play_addr` + `url_expires_at`
- [ ] Checkpoint `max_cursor` **mỗi trang**
- [ ] `sleep(3–5s + jitter)` giữa trang
- [ ] Blocked theo tín hiệu cấu trúc
- [ ] `total_videos` + chuyển `downloading`

## DoD

- [ ] Profile ~30 video: enumerate xong, `total_videos` khớp số row
- [ ] Kill giữa enumerate → restart → tiếp từ `max_cursor`, **không quét lại từ 0**
- [ ] Fake parser trả `awemeList=[]` + `hasMore=1` → `blocked`, `max_cursor` **không đổi**
- [ ] Chạy lại profile đã enumerate → 0 row mới (`ON CONFLICT` hoạt động)
- [ ] `play_addr` + `url_expires_at` có giá trị **ngay sau** enumerate
- [ ] **Mutation:** đăng ký 2 instance lane → test tần suất 2 job **đỏ**
- [ ] **Mutation:** chuyển checkpoint ra sau vòng lặp → test resume **đỏ**
- [ ] **Mutation:** map `blocked` → `failed` → test hồi phục cookie **đỏ**

## Risks

| Risk | Xử lý |
|---|---|
| Ai đó "tối ưu" thành enumerate song song nhiều job | Mutation test tần suất. Comment ghi rõ rate limit theo IP |
| Đoán nguyên nhân từ 400 ngay ở phase này | Chỉ dùng tín hiệu cấu trúc. 400 để phase 7 |
| `sleep` bị hoisted ra ngoài loop | Test đo khoảng cách giữa các request, không chỉ tổng thời gian |
