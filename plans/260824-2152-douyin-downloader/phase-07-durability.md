---
phase: 7
title: "Durability: F4 probe, retry/cancel, bộ mutation test"
status: not_started
effort: ~1.5d
blockedBy: [6]
milestone: M2
---

# Phase 7 — Độ bền · **kết thúc M2**

Phase nặng nhất. Đây là chỗ hệ thống chuyển từ "chạy được" sang "chạy được khi mọi thứ đổ vỡ".

Đọc trước: solution design §1.5 (F4) · §5.2 đường lỗi · `plan.md` §5 (bảng mutation).

---

## 7.1 F4 — probe disambiguation

**Vấn đề.** `app/api/endpoints/douyin_web.py` bắt `except Exception` và trả `ErrorResponseModel` với `message` **mặc định tĩnh** `"An error occurred."`. `str(e)` không bao giờ được truyền vào. Nên bốn nguyên nhân này **giống hệt nhau** từ phía NestJS:

| Nguyên nhân thật | Remediation đúng |
|---|---|
| Cookie bị ban | Push cookie mới → `blocked` |
| `a_bogus` lỗi thời | Pull upstream |
| Profile private / đã xoá | `failed`, thông báo user |
| Parser không ra được Douyin | Retry, chờ mạng |

Làm đúng IDEA.md §6.2 ("3 lỗi liên tiếp → `blocked`") sẽ **báo `blocked` cho profile private**, và người vận hành đi đổi cookie vô ích.

**Giải pháp — ba tầng tín hiệu, không patch parser:**

1. **Tín hiệu cấu trúc** (đã làm ở phase 5): `awemeList` rỗng + `hasMore=1` → `blocked`. HTTP 200, chẩn đoán **chắc chắn**, không cần probe.
2. **3× HTTP 400 liên tiếp → chạy probe**: `fetchUserProfile(PROBE_SEC_USER_ID)` — một profile public đã biết, đặt trong env.
   - probe **fail** → lỗi **toàn cục** (cookie / `a_bogus` / mạng) → `blocked`
   - probe **OK** → riêng profile này → `failed`, `error = "profile không truy cập được, cookie vẫn tốt"`
3. Probe **chỉ chạy trên đường lỗi** ⇒ không đụng ngân sách rate limit đường thường.

**Không patch parser để lấy error text** — sẽ đốt ngân sách `check-patch-size.sh` vào file upstream sở hữu, đúng loại nợ mà `PATCH_NOTES.md` được viết ra để tránh (`plan.md` §0 nguyên tắc 2).

Thay `TODO(phase-7)` ở phase 5 bằng cái này.

## 7.2 Vòng hồi phục đầy đủ

Chuỗi phải chạy end-to-end (solution design §5.4):

```
cookie ban → blocked, max_cursor giữ, videos done vẫn done
  → push cookie mới → parser dùng ngay, KHÔNG restart
  → POST /retry → blocked→pending, videos failed→pending, attempts=0
  → lane tiếp TỪ max_cursor, không làm lại
```

## 7.3 Rate limit test đúng từng pha

- **Enumerate:** 2 job đồng thời → đo tần suất request tổng == 1 luồng (F2)
- **Download:** N video đồng thời → đo có đúng `DOWNLOAD_CONCURRENCY` request bay cùng lúc, không hơn

Hai pha **rate limit khác nhau hoàn toàn** (IDEA.md §3.1). Áp cùng một giới hạn cho cả hai là chậm hơn cần thiết cả chục lần.

## 7.4 Bộ mutation test — bàn giao đủ 10 mutation ở `plan.md` §5

**Luật thi hành:** mỗi mutation phải **in ra diff nó thật sự áp được**. Mutation match rỗng ⇒ **coi như chưa chạy**, không phải "đã pass". Phase 1 sập bẫy này hai lần: `sed` mutation match rỗng sau refactor nên "suite vẫn xanh" là mutation tồi chứ không phải gap thật.

Đưa vào script `scripts/run-mutations.sh`, chạy trong CI. Không để trong đầu người.

---

## Todo

- [ ] `parser.errors.ts`: probe disambiguation (F4)
- [ ] `PROBE_SEC_USER_ID` vào env + zod
- [ ] Đếm 400 liên tiếp per job, reset khi thành công
- [ ] Thay `TODO(phase-7)` ở enumerate lane
- [ ] `/retry` reset `attempts=0` + `blocked→pending`
- [ ] Cancel giữa lúc tải: abort stream, dọn `.tmp`
- [ ] Test rate limit 2 pha
- [ ] `scripts/run-mutations.sh` — 10 mutation, **in diff từng cái**
- [ ] CI chạy mutation script

## DoD

- [ ] Fake parser 400 + probe **OK** → `failed` kèm lý do rõ, **KHÔNG** `blocked`
- [ ] Fake parser 400 + probe **fail** → `blocked`
- [ ] Vòng hồi phục §7.2 chạy đủ, không tải lại video đã done
- [ ] 2 job enumerate đồng thời → tần suất == 1 luồng
- [ ] Download đồng thời == `DOWNLOAD_CONCURRENCY`, không hơn
- [ ] **Cả 10 mutation ở `plan.md` §5 đỏ, mỗi cái in diff áp được**
- [ ] `scripts/run-mutations.sh` chạy trong CI, fail build nếu mutation nào **không** đỏ
- [ ] Fail build nếu mutation nào áp **rỗng**

## Risks

| Risk | Xử lý |
|---|---|
| `PROBE_SEC_USER_ID` trỏ profile đã chết → probe luôn fail → mọi thứ thành `blocked` | Health check lúc bootstrap: probe phải OK khi cookie tốt. Fail fast nếu không |
| Probe đốt rate limit | Chỉ chạy sau 3× 400. Có test đếm |
| Mutation script match rỗng mà vẫn báo pass | Script **fail** nếu diff rỗng. Đây là bài học đắt nhất của phase 1 |
| Đếm 400 không reset khi thành công | Job dài sẽ tích đủ 3 lỗi rải rác rồi báo blocked sai. Có test |
