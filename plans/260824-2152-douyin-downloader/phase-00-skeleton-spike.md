---
phase: 0
title: Skeleton + hai spike gate
status: completed
effort: ~0.5d
blockedBy: []
milestone: GATE
---

# Phase 0 — Skeleton + hai spike gate

**Không có tính năng nào cho người dùng.** Mục đích duy nhất: dựng chỗ để code, và **trả lời hai giả định về thư viện/hợp đồng bên ngoài bằng bằng chứng chạy được**.

Đọc trước: `plan.md` §3 (gate) · solution design §6 (ORM) §8 (side effects).

---

## 0.1 Monorepo skeleton

```
pnpm-workspace.yaml          packages: ["services/*"]
package.json                 root, chỉ devDeps dùng chung
tsconfig.base.json
docker-compose.yml
.env.example
services/downloader/         NestJS, boot rỗng, chỉ /health
src/Douyin_TikTok_Download_API/   submodule — KHÔNG SỬA
```

## 0.2 `docker-compose.yml` — 4 service + web sau

| Service | Ghi chú bắt buộc thành comment trong file |
|---|---|
| `nest` | mount `DATA_DIR` |
| `parser` | **KHÔNG có `ports:`** — không expose ra ngoài internal network. Cần `REDIS_URL` + `DOUYIN_COOKIE_FALLBACK`. `requirements.txt` của fork đã pin `redis` |
| `postgres` | |
| `redis` | Phải sẵn **trước** parser. Không có Redis thì cookie rơi về `DOUYIN_COOKIE_FALLBACK` |

Hai ràng buộc trên sẽ **vỡ âm thầm** nếu ai xoá — ghi comment ngay tại chỗ, không chỉ trong doc.

## 0.3 `.env.example` + zod validate, fail-fast lúc bootstrap

```
API_KEY · DATABASE_URL · REDIS_URL · PARSER_BASE_URL · DATA_DIR=/data
DOWNLOAD_CONCURRENCY=6 · ENUMERATE_SLEEP_MS_MIN=3000 · ENUMERATE_SLEEP_MS_MAX=5000
LEASE_SECONDS=300 · MAX_ATTEMPTS=3 · URL_TTL_SECONDS=7200
PROBE_SEC_USER_ID          # profile public đã biết, dùng cho F4 probe (phase 7)
DISK_RESERVE_BYTES=10737418240 · EST_BYTES_PER_VIDEO=31457280
```

Thiếu biến ⇒ **crash lúc khởi động**, không phải lỗi lúc chạy job đầu tiên.

---

## 0.4 SPIKE 1 (GATE) — `FOR UPDATE SKIP LOCKED` trong Drizzle

**Câu hỏi:** diễn đạt được **không cần rơi xuống raw SQL**?

**Chứng minh — không đọc doc, chạy test:**
1. Bảng tạm `spike_items(id, status)`, chèn 1 row `pending`.
2. Transaction A: claim row bằng Drizzle `.for('update', { skipLocked: true })`, **giữ transaction mở**.
3. Transaction B song song: chạy cùng câu claim.
4. **Khẳng định B trả về rỗng NGAY** — không chờ, không timeout, không lấy được row của A.

Bước 4 là toàn bộ ý nghĩa của spike. Một test chỉ kiểm "câu SQL chạy không lỗi" **không chứng minh gì** — đó đúng loại false-green đã sập hai lần ở phase 1.

**Không đạt ⇒ đổi ORM NGAY TẠI ĐÂY**, trước khi có dòng domain code nào. TypeORM (`setLock` + `setOnLocked('skip_locked')`) là phương án dự bị. Prisma đã bị loại (cần `$queryRaw`).

## 0.5 SPIKE 2 (GATE) — NestJS ghi Redis → parser dùng cookie mới, không restart

Hợp đồng này đã implement ở fork nhưng **chưa bao giờ verify từ phía Node**.

1. Node ghi `douyin:cookie:current` = giá trị sentinel A.
2. Gọi một route parser, khẳng định request đi kèm A.
3. Ghi giá trị B. **Không restart parser.**
4. Gọi lại, khẳng định request đi kèm B.
5. **Xoá key** → khẳng định parser không crash, rơi về `DOUYIN_COOKIE_FALLBACK`.

Journal phase 1 ghi rõ: integration test "2 event loop trong lifecycle parser thật" **vẫn chưa tồn tại**. Bước 3–4 dưới tải đồng thời là chỗ gần nhất ta có với nó — chạy 5 request song song trong lúc đổi key.

---

## Todo

- [x] pnpm workspace + tsconfig base
- [x] `services/downloader` boot, `GET /health`
- [x] `docker-compose.yml` 4 service, parser **không** `ports:`, comment 2 ràng buộc
- [x] `.env.example` + zod fail-fast
- [x] **SPIKE 1** — test 2 transaction song song, B trả rỗng ngay
- [x] **SPIKE 2** — đổi key giữa lúc 5 request đang bay, không restart
- [x] CI: thêm job `services/downloader` (lint + test). **Giữ nguyên** 3 gate cũ: gitleaks canary · `check-patch-size.sh` · pytest fork

## DoD

- [x] `docker compose up` từ máy sạch: 4 container khoẻ
- [x] `curl` parser **từ ngoài host thất bại**, từ container `nest` thành công
- [x] Spike 1 xanh **và** mutation: bỏ `skipLocked` → test **đỏ**
- [x] Spike 2 xanh **và** mutation: xoá key → không crash
- [x] Ba gate CI cũ vẫn xanh

## Risks

| Risk | Xử lý |
|---|---|
| Spike 1 fail | Đổi ORM tại đây. Đây chính là lý do nó là gate, không phải task |
| Spike 2 fail | Toàn bộ tự động hoá cookie sụp ⇒ dừng, điều tra fork, **không** đi tiếp phase 1 |
| "Spike xanh" nhưng test không kiểm đúng thứ cần | Mỗi spike có mutation đi kèm ở DoD. Không có mutation ⇒ spike **chưa xong** |

---

## Outcome

Commit `0b09d8c feat(downloader): scaffold service and clear both phase 0 gates`. Cả hai gate **PASS**, verify độc lập + mutation.

- **SPIKE 1 PASS.** Drizzle diễn đạt `FOR UPDATE SKIP LOCKED` không raw SQL, đúng shape `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING` phase 1 cần. Chứng minh bằng 2 transaction Postgres thật sự chạy song song. Cả 2 mutation bắt buộc (bỏ `skipLocked`; bỏ khoá hẳn) đều làm cả 4 test đỏ. **Quyết định ORM chốt: Drizzle, không đổi TypeORM.**
- **SPIKE 2 PASS.** Parser fork gốc (không sửa) nhận cookie mới từ Redis ở request kế tiếp, không restart, dưới tải đồng thời; xoá key thì rơi về `DOUYIN_COOKIE_FALLBACK`. 3 mutation (sai key, Redis không tới được, restart worker) đều làm gate đỏ rồi phục hồi xanh.

**5 false-green bị bắt và sửa trước khi commit** (cùng loại lỗi đã ghi ở `docs/journals/20260824-phase-01-false-greens.md`):
1. 2 test SKIP LOCKED đặt tên "concurrency" nhưng pass cả khi bỏ khoá — `Promise.all` trên transaction Drizzle chưa từng chồng lấp nhau thật. Chỉ thêm barrier vẫn chưa đủ: `LockRows` của Postgres tự thử row tiếp theo, tự "chữa lành" một `SKIP LOCKED` bị thiếu khi commit nhanh — nên claim thắng phải giữ mở tới khi mọi contender kết thúc.
2. Test "API_KEY phải bí mật" dùng key **hợp lệ** sẵn nên không thể fail.
3. `API_KEY` và `PROBE_SEC_USER_ID` có thể hạ xuống optional-default-`""` mà cả suite vẫn xanh. `API_KEY` sẽ là credential `X-Api-Key` ở phase 3 ⇒ đây là primitive auth-bypass.
4. Bước "5 request song song giữa lúc đổi cookie" thực ra không đồng thời, không mid-flight.
5. Guard chống-restart theo dõi PID 1 container (`/bin/sh`), không phải worker uvicorn phục vụ request — đúng failure mode nó phải bắt lại là cái để lọt xanh.

Ngoài ra: bootstrap chưa có positive control (`main.ts` không bao giờ boot vẫn pass hết test); lint chưa có config, chưa từng chạy; log capture-cookie của parser suýt vào git làm cookie sink; `nest` nhận nguyên `.env` gồm cả cookie fallback và mật khẩu DB; container downloader chạy root. Tất cả đã sửa trước commit.

**Carry-forward sang phase sau** — xem `plan.md` §5, §6, §8.
