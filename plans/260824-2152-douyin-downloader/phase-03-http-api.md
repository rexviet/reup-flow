---
phase: 3
title: HTTP API + cookie endpoint + boundary enforcement
status: completed
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

- [x] `api-key.guard.ts` (D8), áp cho cả `/api` và `/internal`
- [x] `jobs.controller/service/repository` — 5 route
- [x] `videos.controller` — browse paginate
- [x] `cookie.controller/service` — ghi 2 key Redis
- [x] `redact.logger.ts`
- [x] ESLint `no-restricted-imports` + test kiến trúc
- [x] Test: `retry` reset `attempts=0`

## DoD

- [ ] `POST /api/jobs` với URL đầy đủ **và** short link → cùng một `sec_user_id` — *unit test dùng `FakeParser`, không phải live parser; coverage thật cần `pnpm test:integration`*
- [x] Thiếu/sai `X-Api-Key` → 401 trên **mọi** route, gồm `/internal`
- [x] `GET /internal/cookie/status` **không** chứa giá trị cookie ở bất kỳ field nào
- [x] `age_seconds` đúng sau khi push 2 lần (chứng minh đọc `pushed_at`, không đọc TTL)
- [ ] Test kiến trúc xanh: row nạp bằng SQL thuần vẫn được lane xử lý — *lane chưa tồn tại; test hiện chứng minh `jobsRepository.claimOne` claimable + require-cache kiểm không load lane module; carry real DoD sang phase 4*
- [x] **Mutation:** thêm `logger.log(cookie)` → test **đỏ**
- [x] **Mutation:** thêm `console.log(cookie)` → test **đỏ** *(riêng biệt — đây chính là cái lọt ở phase 1)*
- [x] **Mutation:** cho `jobs.service` `import` lane → ESLint **đỏ**

## Risks

| Risk | Xử lý |
|---|---|
| Controller gọi lane cho "tiện" | ESLint + test kiến trúc. Vi phạm là fail build, không phải review comment |
| `status` suy tuổi cookie từ TTL | TTL refresh mỗi push ⇒ luôn báo "mới". DoD có test 2 lần push |
| Cookie vào `error` của job rồi ra API response | Redact phủ cả đường ghi `jobs.error` |
| `retry` quên reset `attempts` | Video failed không bao giờ được claim lại. Có test |

---

## Outcome

**DONE.** 331 test xanh (34 suite), `pnpm lint` + `pnpm build` + `gitleaks` đều sạch. `test/mutations/phase-03.sh` có 20 mục: **17 mutation bị test bắt, 1 bị ESLint bắt, 2 control phải-xanh vẫn xanh**.

Kiến trúc: `AppModule` gắn `ApiKeyGuard` làm `APP_GUARD`, nên **mọi** route được phủ mặc định và `@Public()` là lối thoát duy nhất — chỉ `/health` dùng, vì healthcheck của compose gọi nó bằng `wget` không header. Bootstrap tách ra `src/bootstrap.ts` để test leak chạy **đúng** đường bootstrap của production, không phải một bản dựng lại na ná.

### Redaction: ba lối thoát mà suite xanh không thấy

Review đối kháng sau khi implement tìm ra ba đường một cookie vẫn đi ra được, cả ba đều nằm dưới một suite đang xanh:

1. **Chunk `Uint8Array` đi thẳng qua stdio patch.** `Buffer.isBuffer()` trả false cho `Uint8Array` thuần, nên nhánh cuối forward nguyên xi. Bất cứ thứ gì `TextEncoder().encode(...)` hoặc pipe một web stream vào stdout đều leak. Sửa: `ArrayBuffer.isView()`.
2. **Pattern đòi `=` literal**, nên trượt `{"sessionid":"..."}` — **chính là shape mà nhánh `JSON.stringify` trong `redact.logger.ts` tự sinh ra**. Test tưởng phủ ca này thực ra không: fixture để cookie ở dạng `field=value` *bên trong* một string value, nên pattern cũ khớp vì lý do sai. Sửa: nhận cả `=` lẫn `:`, quote tuỳ chọn, charset gồm base64 (`+`, `/`, `=`).
3. **Secret bị cắt đôi giữa hai lần `write()` thì leak** — và tệ hơn: khi vết cắt rơi vào giữa value, phần đầu vẫn được redact nên output *trông như đã redact* trong khi phần đuôi nằm nguyên bên cạnh. Sửa: line buffer theo từng stream. Khai thác đúng một tính chất — **secret ở đây không bao giờ chứa `\n`** — nên mọi thứ tới `\n` cuối cùng flush ngay, chỉ giữ lại dòng dở. Không có độ trễ cho log theo dòng. `pending` chặn ở 64 KiB để một dòng dài vô hạn không ăn hết bộ nhớ.

Redaction giờ giữ nguyên dấu phân cách khi thay: `{"sessionid":"..."}` → `{"sessionid":"[redacted]"}`, vẫn là JSON parse được. Bản đầu chuẩn hoá mọi thứ về `field=[redacted]`, tức là gỡ được secret nhưng **phá luôn bản ghi** — đổi một kiểu mù này lấy một kiểu mù khác.

### Hai test không thể đỏ vì lý do chúng khai

Đúng loại lỗi phase 1 và 2 đã dính, lần này bắt trước khi commit:

- **Assertion "response body không chứa cookie" đọc bằng chứng từ stdout đã bị redact.** Nó không kiểm HTTP layer, nó kiểm lại chính cái stdio patch mà ba test khác đã kiểm. Sửa: harness ghi body thô ra **file** (patch không đụng tới file), spec đọc file đó. Đã chứng minh đỏ bằng đúng mutation của review trước khi nhận là xong.
- **Ba assertion `not.toHaveProperty('lease_expires_at')`** canh một key mà code **không bao giờ** phát ra: thuộc tính Drizzle là `leaseExpiresAt`. Spread nguyên row ORM vào response thì ba assertion đó vẫn xanh. Sửa: khẳng định trên body đã serialise, `/lease.?expires/i`, bắt cả hai cách viết.

### `jobs.error` / `videos.last_error` — risk row của chính phase này, chưa làm

§6 giao cho phase 3: *"Cookie vào `error` của job rồi ra API response — redact phủ cả đường ghi `jobs.error`"*. Không có dòng redaction nào trên đường ghi lẫn đường đọc, mà cả hai cột đều được trả thẳng cho client. Phase 3 chưa ghi vào chúng nên chưa hỏng gì — nhưng phase 6/7 sẽ ghi text chịu ảnh hưởng của bên ngoài vào đó. Giờ redact **cả hai** đường: đường ghi giữ secret khỏi chính database (một người cầm `psql`, một bản backup), đường đọc phủ những row do đường khác ghi vào (migration, UPDATE tay, một repository function tương lai).

### SSRF có xác thực ở `POST /api/jobs`

Endpoint nhận **mọi** URL rồi đưa thẳng cho parser, và parser **đi theo** link đó. Container parser cố ý không có `ports:` (xem comment trong `docker-compose.yml`) — endpoint này phát lại đúng khả năng với tới mạng nội bộ mà thiết kế kia dựng ra để chặn. Giờ allowlist host: `douyin.com`, `iesdouyin.com` và subdomain.

### Control phải-xanh tìm ra lỗi mà không test nào bắt

`CookieService.push` đăng ký cookie với redactor ở **câu lệnh cuối** của method. Toàn bộ thân method vì thế là một cửa sổ mà value đã sống nhưng chưa redact được — và thứ dễ ghi trong cửa sổ đó nhất chính là đường lỗi: ioredis thường nhúng tham số nó fail vào message, rồi caller ghi ra stderr. Suite xanh hoàn toàn; **control (thứ phải-XANH) là cái đỏ lên**. Giờ `registerSecret` là việc đầu tiên sau khi validate platform, và có test riêng bắt buộc: ép lệnh Redis ném lỗi, cookie vẫn phải bị scrub.

Đây đúng là điều luật thứ hai ở §5 dựng ra để làm — nhưng theo một hướng không lường trước: control không chỉ nói *cơ chế nào* đang gánh invariant, nó còn phát hiện được *thứ tự* trong một method.

### Harness tự nó có hai lỗi — xem §5 luật ba và bốn

Cả hai đều làm suite báo cáo sai mà vẫn trông hợp lý; chi tiết ở `plan.md` §5.

### Còn nợ

- **Chunk-boundary vẫn hở trong một trường hợp:** một dòng dài quá 64 KiB không có `\n` sẽ bị flush phần đầu, chấp nhận rủi ro cắt giữa secret để không ăn bộ nhớ vô hạn. Đánh đổi có chủ ý, ghi rõ trong `stdio-redaction.ts`.
- **Registry chặn ở 32 giá trị** (FIFO). Cookie rất cũ xuất hiện trong một log rất trễ sẽ không còn được scrub — đổi lấy việc không phình vô hạn và không làm mỗi lần ghi stdout chậm dần theo uptime (extension ở phase 9 push mỗi 20 phút, `msToken`/`ttwid` xoay nên phần lớn push là giá trị mới).
- **Hai dòng DoD ở trên cố ý để trống**, lý do ghi ngay tại chỗ. Bản thật của test kiến trúc đã chuyển sang `phase-04-lane-framework.md`.
