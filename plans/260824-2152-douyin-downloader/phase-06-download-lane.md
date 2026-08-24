---
phase: 6
title: Download lane + M1 gate
status: not_started
effort: ~1d
blockedBy: [5]
milestone: "M1 (gate)"
---

# Phase 6 — Download lane · **kết thúc M1**

Đọc trước: solution design §5.3 (sequence) · `plan.md` §2 (F1, F6) · §7 (lệch IDEA.md §3.2).

---

## 6.1 N loop song song

`DOWNLOAD_CONCURRENCY` (mặc định 6) instance `DownloadLane`, mỗi cái một loop độc lập.

**Concurrency = số loop, không phải semaphore.** Cấu trúc bảo đảm không vượt N. CDN `*.douyinvod.com` không chịu risk control như API nên 4–8 luồng là an toàn — **khác hoàn toàn** enumerate (IDEA.md §3.1).

## 6.2 Mỗi lượt

```
1. claim 1 video (SKIP LOCKED), lease +5m, attempts+1        ← F6
2. job của video này đã cancelled? → skipped, xong
3. url_expires_at còn tươi?
     có  → dùng luôn                    ← 0 request API, ĐƯỜNG THƯỜNG (F1)
     hết → parser.fetchOneVideo(aweme_id), cập nhật play_addr + url_expires_at
4. GET play_addr, header Referer=https://www.douyin.com/ + UA khớp cookie
     403/410 → re-resolve 1 lần rồi thử lại (URL chết sớm hơn TTL)
5. stream → .tmp (tên duy nhất theo lượt claim)
6. verify content-length
7. KIỂM LEASE còn hiệu lực → fs.rename → status=done, storage_path
8. còn video pending/downloading cho profile này? hết → job.status=completed
```

**Bước 7 kiểm lease trước `rename` là bắt buộc.** Nếu lease đã hết (reaper đã nhả video cho worker khác) thì worker này phải **tự huỷ và xoá `.tmp`**, không được `rename`. Không có bước này thì hai worker cùng commit một file — risk đã ghi ở solution design §10.

**F1 là lệch có chủ ý so với IDEA.md §3.2.** Ghi comment tại chỗ: làm đúng §3.2 (re-resolve mọi video) tốn **525 request** cho profile 500 video thay vì 25 ⇒ vượt 3–5× hạn mức/giờ ⇒ ban gần như chắc chắn. Cache + re-resolve **phản ứng** vẫn không tin URL cũ, chỉ không kiểm tra mù.

## 6.3 Retry

3 lần, exponential backoff. Quá → `status=failed`, ghi `last_error`, **đi tiếp, không kill cả job**. `attempts` đã tăng lúc claim (F6) nên chặn trên là cứng.

---

## Todo

- [ ] `download.lane.ts`, N instance
- [ ] Claim + lease + heartbeat cho file lớn
- [ ] F1: dùng URL cache, re-resolve khi hết hạn **hoặc** 403/410
- [ ] Stream → `.tmp` duy nhất → verify → **kiểm lease** → `rename`
- [ ] Kiểm job cancelled trước và trong lúc tải
- [ ] Finalize job khi hết việc
- [ ] Retry 3 lần backoff → `failed`, không kill job

## DoD — đồng thời là **M1 gate**

- [ ] **Tải xong 1 profile <50 video từ đầu đến cuối**, số file trên đĩa khớp `downloaded_count`
- [ ] `URL_TTL_SECONDS` đủ dài → **0 lần gọi `fetchOneVideo`** (đếm request, chứng minh F1 hoạt động)
- [ ] Đặt `url_expires_at` vào quá khứ → re-resolve rồi tải được
- [ ] Fake CDN trả 403 → re-resolve 1 lần rồi thành công
- [ ] Kill process giữa download → restart → **không tải lại file đã done**
- [ ] Cancel giữa lúc tải → dừng, `.tmp` được dọn, không có file cụt
- [ ] Không có `.tmp` sót lại sau khi mọi job xong
- [ ] **Mutation:** bỏ kiểm lease trước `rename` → test 2-writer **đỏ**
- [ ] **Mutation:** bỏ verify content-length → test file cụt **đỏ**

## Risks

| Risk | Xử lý |
|---|---|
| Reaper nhả video, 2 worker commit 1 file | Kiểm lease trước `rename` + `.tmp` tên duy nhất. Mutation test |
| Ai đó "sửa cho đúng IDEA.md §3.2" | Comment tại chỗ ghi con số 525 vs 25 + DoD đếm request |
| Job không bao giờ `completed` vì finalize thua đua | Reaper (phase 4) là lưới an toàn |
| File lớn vượt lease giữa lúc tải | Heartbeat lease trong lúc stream |
