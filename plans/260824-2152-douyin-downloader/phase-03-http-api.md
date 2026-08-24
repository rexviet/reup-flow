---
phase: 3
title: HTTP API + cookie endpoint + boundary enforcement
status: not_started
effort: ~1d
blockedBy: [2]
milestone: M1
---

# Phase 3 — HTTP API + cookie + ranh giới

Đây là phase thi hành **nguyên tắc 1** (`plan.md` §0): HTTP layer **không bao giờ** gọi lane. Controller chỉ ghi row.

Đọc trước: solution design §5.1 (ranh giới) · §7 (ESLint) · brainstorm §9 (endpoint list).

---

## 3.1 Endpoints

```
POST   /api/jobs                            { url } -> { job_id }
GET    /api/jobs                            list, paginate
GET    /api/jobs/{id}                       detail + counters
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/retry                 blocked→pending; videos failed→pending, attempts=0
GET    /api/profiles/{sec_user_id}/videos   paginate

POST   /internal/cookie                     { platform, cookie }   <- extension
GET    /internal/cookie/status              { present, age_seconds, last_ok_at }
```

**Không có `/events` SSE** — polling 1s (D6), lệch có chủ ý so với IDEA.md §5.2.

`POST /api/jobs` làm đúng ba việc: `resolveSecUserId(url)` qua parser → upsert `profiles` → insert `jobs` ở `pending`. **Xong. Không gọi lane.** Lane sẽ tự nhặt row.

`retry` reset `attempts=0` cho video `failed` — nếu không thì `attempts < max_attempts` trong claim query (phase 1) sẽ loại chúng ngay.

## 3.2 Cookie — ghi Redis, không bao giờ đọc để trả về

`POST /internal/cookie` → `SET douyin:cookie:current` TTL 24h **và** `SET douyin:cookie:pushed_at`.

**Key thứ hai là bắt buộc, không phải tiện lợi.** TTL bị refresh mỗi lần push nên **TTL không nói gì về tuổi cookie**. `status` phải đọc `pushed_at`.

`GET /internal/cookie/status` trả `{present: bool, age_seconds, last_ok_at}` — **không bao giờ** trả giá trị cookie.

## 3.3 Redaction — ở tầng logger, không ở call site

`redact.logger.ts` bọc Nest logger, lọc mọi thứ khớp pattern cookie trước khi ghi.

**Lý do phải ở tầng logger:** phase 1 đã học rằng redact từng call site sẽ hụt đúng cái call site mà người ta quên. Và test phải phủ **cả hai đường**:
- logging subsystem
- **stdout/stderr trực tiếp** — leak lịch sử ở fork là `print()`, không phải logger. Test cũ chỉ nhìn logger nên `print()` lọt qua sạch.

## 3.4 Thi hành ranh giới — hai tầng

1. **ESLint** `no-restricted-imports` trong `jobs/` + `videos/` + `cookie/`: cấm `**/lanes/**`. Bắt lúc gõ.
2. **Test kiến trúc**: nạp tay một row `jobs` ở `pending` bằng SQL thuần (không qua API) → lane vẫn chạy đủ. Chứng minh hai bên chỉ nối qua DB. Bắt trường hợp lách bằng dynamic import.

Giữ **cả hai**, không chọn một.

---

## Todo

- [ ] `api-key.guard.ts` (D8), áp cho cả `/api` và `/internal`
- [ ] `jobs.controller/service/repository` — 5 route
- [ ] `videos.controller` — browse paginate
- [ ] `cookie.controller/service` — ghi 2 key Redis
- [ ] `redact.logger.ts`
- [ ] ESLint `no-restricted-imports` + test kiến trúc
- [ ] Test: `retry` reset `attempts=0`

## DoD

- [ ] `POST /api/jobs` với URL đầy đủ **và** short link → cùng một `sec_user_id`
- [ ] Thiếu/sai `X-Api-Key` → 401 trên **mọi** route, gồm `/internal`
- [ ] `GET /internal/cookie/status` **không** chứa giá trị cookie ở bất kỳ field nào
- [ ] `age_seconds` đúng sau khi push 2 lần (chứng minh đọc `pushed_at`, không đọc TTL)
- [ ] Test kiến trúc xanh: row nạp bằng SQL thuần vẫn được lane xử lý
- [ ] **Mutation:** thêm `logger.log(cookie)` → test **đỏ**
- [ ] **Mutation:** thêm `console.log(cookie)` → test **đỏ** *(riêng biệt — đây chính là cái lọt ở phase 1)*
- [ ] **Mutation:** cho `jobs.service` `import` lane → ESLint **đỏ**

## Risks

| Risk | Xử lý |
|---|---|
| Controller gọi lane cho "tiện" | ESLint + test kiến trúc. Vi phạm là fail build, không phải review comment |
| `status` suy tuổi cookie từ TTL | TTL refresh mỗi push ⇒ luôn báo "mới". DoD có test 2 lần push |
| Cookie vào `error` của job rồi ra API response | Redact phủ cả đường ghi `jobs.error` |
| `retry` quên reset `attempts` | Video failed không bao giờ được claim lại. Có test |
