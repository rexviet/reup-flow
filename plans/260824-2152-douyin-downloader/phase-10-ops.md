---
phase: 10
title: "Ops: preflight đĩa, CI upstream, polish"
status: not_started
effort: ~0.5d
blockedBy: [7, 8]
milestone: M4
---

# Phase 10 — Vận hành · **kết thúc M4**

Đọc trước: solution design §11 (Q3, Q4) · IDEA.md §11.

---

## 10.1 Preflight dung lượng đĩa (Q3 — đã trả lời)

Chạy **sau enumerate, trước download** — lúc đó mới biết `total_videos`.

```
cần = total_videos × EST_BYTES_PER_VIDEO      (mặc định 30MB)
nếu cần > storage.freeBytes() − DISK_RESERVE_BYTES  (mặc định 10GB)
   → job.status = 'failed', error nói rõ cần bao nhiêu / còn bao nhiêu
```

30MB là **biên trên** của khoảng 5–30MB ở IDEA.md §11. 10GB là đệm để máy không chết vì đầy đĩa. Cả hai qua env.

Không chạy lúc tạo job: lúc đó `total_videos` còn NULL, ước lượng sẽ là số bịa.

## 10.2 CI pull upstream hàng tuần (Q4 — đã trả lời)

GitHub Actions, cron hàng tuần:

```
fetch upstream → check-patch-size.sh → pytest fork
  → grep 'headers\["Cookie"\]|headers\.Cookie' crawlers/douyin/web/utils.py
  → lệch? mở issue
```

**Vì sao là CI chứ không phải nhắc người.** `PATCH_NOTES.md` tự ghi rằng nó chỉ mang tính khuyến nghị và **không được CI enforce**. IDEA.md §11 nói "đặt lịch kiểm hàng tuần" — dựa vào con người nhớ, và con người sẽ không nhớ. `a_bogus` phải chạy theo Douyin; pull chậm là job `blocked` hàng loạt.

Grep `utils.py` là bẫy đã ghi ở `PATCH_NOTES.md` §5: nếu upstream cho `TokenManager` đọc `headers.Cookie` thì patch **springs a silent leak** — không lỗi, không crash, request đơn giản dùng cookie rỗng trong `config.yaml` trong khi Redis có cookie tốt.

## 10.3 Alert vận hành

| Alert | Vì sao |
|---|---|
| Lane không tick > 5 phút | Lane chết âm thầm (phase 4) |
| Tỉ lệ job `blocked` tăng | Cookie ban **hoặc** `a_bogus` lỗi thời **hoặc** upstream hoist header. Ba nguyên nhân, cùng một triệu chứng — **watch cái này, không watch exception** |
| Đĩa dưới ngưỡng | |
| Asset đứng yên > 6h | Job kẹt mà reaper không nhặt được |

## 10.4 Browse polish

`GET /api/profiles/{sec_user_id}/videos` đã có từ phase 3. Thêm UI list video đã tải: tên, thời lượng, kích thước, đường dẫn.

**Preview video trong UI: KHÔNG (Q2 — hoãn, mặc định không).** v1 chỉ trả `storage_path`. Thêm serve static sau = một route Nest, additive, không breaking. Không xây trước.

## 10.5 `DATA_DIR` (Q1 — hoãn)

Vẫn hoãn: mount ở đâu, quota bao nhiêu là quyết định lúc deploy. `Storage.freeBytes()` đọc lúc chạy; thiết kế không giả định con số nào. Ghi vào README phần deploy.

---

## Todo

- [ ] Preflight sau enumerate, trước download
- [ ] `EST_BYTES_PER_VIDEO` + `DISK_RESERVE_BYTES` vào env + zod
- [ ] `.github/workflows/upstream-check.yml` cron hàng tuần
- [ ] 4 alert §10.3
- [ ] UI list video đã tải
- [ ] README: deploy + `DATA_DIR` + load extension

## DoD

- [ ] Giả `freeBytes()` nhỏ → job `failed` kèm số cụ thể, **không** bắt đầu download
- [ ] Preflight chạy **sau** enumerate (test: `total_videos` phải có giá trị lúc kiểm)
- [ ] Workflow upstream chạy tay được, mở issue khi patch lệch
- [ ] Grep `utils.py` fail build khi có hit
- [ ] Alert lane-không-tick kích hoạt được bằng cách kill 1 lane
- [ ] README: máy sạch → `docker compose up` → tải xong 1 profile

## Risks

| Risk | Xử lý |
|---|---|
| Ước lượng 30MB/video sai với profile video dài | Biên trên + đệm 10GB. Env đổi được. Preflight là hàng rào, không phải dự báo chính xác |
| CI upstream ồn (issue mỗi tuần) | Chỉ mở issue khi **thật sự lệch**, không phải mỗi lần upstream có commit |
| Preflight chặn job hợp lệ | Log rõ cần/còn bao nhiêu; env hạ được ngưỡng |
