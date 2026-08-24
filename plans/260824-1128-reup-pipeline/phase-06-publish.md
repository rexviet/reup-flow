---
phase: 6
title: Module 3 — Publish lên Facebook (M4)
status: not_started
effort: 4 ngày
blockedBy: [5]
---

# Phase 6 — Module 3: Publish

mp4 đã duyệt → hẹn giờ lên fanpage qua Graph API.

Kết thúc phase này: **bài tự lên page theo lịch.**

> **Tiền đề từ Phase 0 S2:** đã xác nhận `expires_at` của page token dài hạn có hình dạng gì, và bài đăng qua `/videos` **có vào tab Reels**. Nếu S2 bước 8 fail thì phase này đi theo D3-A3 (hai code path) và effort gấp đôi — xem §6.8.

---

## 6.1 Nguyên tắc: phase này **không được import gì từ `libs/dubbing`**

Module 3 không biết mp4 tới `approved` bằng cách nào — **nạp tay cũng phải chạy**. ESLint chặn (Phase 2), `module-isolation` test bảo vệ.

Test đó là thứ duy nhất bắt được vi phạm nguyên tắc kiến trúc số 1 nếu ai đó lách bằng dynamic import.

---

## 6.2 `fb.onboarding.ts` — D6-A1.5, dán **một** ô

```
Người dùng (một lần, thủ công)
  Graph API Explorer → chọn app → tick pages_show_list + pages_read_engagement
                                       + pages_manage_posts → Generate Access Token
  → copy user token ngắn hạn (~1 giờ)
        │
        ▼  dán vào tool, MỘT ô
Tool (tự động, server-side)
  1. GET /oauth/access_token?grant_type=fb_exchange_token
       &client_id=FB_APP_ID&client_secret=FB_APP_SECRET&fb_exchange_token=<đã dán>
     → user token dài hạn (60 ngày)
  2. GET /me/accounts?fields=id,name,access_token
     → TOÀN BỘ page người đó quản lý + page token KHÔNG HẾT HẠN, một lời gọi
  3. upsert fanpages, mã hoá AES-256-GCM, token_never_expires = true
  4. lưu long-lived user token (mã hoá) vào fb_credentials
```

### Ba điều dễ hiểu sai — viết vào comment của file này

1. **60 ngày là thời hạn của USER token, không phải page token.** Page token sinh ra từ user token dài hạn **không có ngày hết hạn**. User token hết hạn sau 60 ngày **không làm rớt việc đăng bài** — nó chỉ cần còn hiệu lực nếu muốn gọi lại `/me/accounts` để đồng bộ page mới.
2. **"Không có ngày hết hạn" ≠ bất tử.** Vẫn bị vô hiệu khi: đổi mật khẩu · thu hồi quyền app · mất vai trò admin trên page · Meta phát hiện bất thường. Vì vậy **vẫn cần health check hàng ngày**.
3. **Không có refresh endpoint cho page token.** Muốn token mới phải đổi user token mới rồi gọi lại `/me/accounts`.

### Không cần App Review

Cổng duy nhất là **scope của bước 1**, và Standard Access đủ cho page mà admin/developer/tester của app quản lý. App để **Development mode** — nó chỉ hoạt động cho người có vai trò trên app, mà đó chính xác là định nghĩa của một internal tool.

`fb_exchange_token`, `/me/accounts`, `POST /{page-id}/videos` **không** bị App Review chặn — chúng không phải permission.

---

## 6.3 `token.store.ts` — validate nhị phân, không phải ngưỡng

```
GET /debug_token?input_token=<page token>&access_token=<app token>
  → đọc expires_at, scopes, type, profile_id/page_id
```

**Chỉ nhận token có `expires_at == 0` (hoặc vắng mặt).** Từ chối mọi token có `expires_at` là một thời điểm trong tương lai.

> Thời hạn page token là **NHỊ PHÂN**: hoặc vài giờ (sinh từ user token ngắn hạn), hoặc không hết hạn (sinh từ user token dài hạn). **Không có trường hợp "còn 5 ngày".** Nên phép kiểm là nhị phân, không phải ngưỡng — code so sánh với một ngưỡng ngày là code hiểu sai vấn đề.

Với A1.5, **tool tự exchange nên nó biết chắc token thuộc loại đúng** ⇒ phép kiểm này chuyển từ *chặn lỗi người dùng* thành *assertion nội bộ*. Giữ nó, vì `PastedPageTokenProvider` (đường lùi) vẫn cần nó thật sự chặn.

### Lưu trữ

- `fanpages.access_token_enc BYTEA` — **AES-256-GCM**, key từ `ENCRYPTION_KEY` env.
- **Không bao giờ** log token, **không bao giờ** trả token trong API response.
- `FB_APP_SECRET` và `ENCRYPTION_KEY` **chỉ ở env**, không vào DB, **không vào frontend**.

> Page token ở **Postgres** chứ không Redis vì hai secret có hai vòng đời khác nhau: cookie Douyin là **ephemeral** (TTL 24h, extension tự refresh) ⇒ Redis đúng. Page token là **config bền** (không hết hạn, dán tay, phải sống qua restart và nằm trong backup) ⇒ Postgres đúng. Mã hoá để một dump DB không làm rò 100 token.

### `jobs/check-tokens.job.ts`

`@Cron` hàng ngày → `fanpages.token_status ∈ {ok, expiring, invalid}` + badge đỏ per page trên UI.

---

## 6.4 `distributors/round-robin.ts`

```ts
pick(rendition, pages) → page active có ÍT publication NHẤT
```

**Không dùng cursor `lastIndex`.** Least-loaded cho kết quả round-robin ở trạng thái ổn định nhưng **tự lành**:
- Thêm page → nó được nạp trước cho tới khi bằng nhau
- Tắt page → phần còn lại tự chia

Cursor sẽ lệch mỗi lần danh sách page đổi và **không có cách phát hiện**.

**Phải đếm trên bảng `publications`, không trên một counter.** Counter lệch khi có cancel/delete và không tự sửa.

---

## 6.5 `policy/slots.ts`

```
next_slot(fanpage) =
  khe trống kế tiếp theo posts_per_day + post_hours,
  bỏ qua slot đã dùng,
  TÔN TRỌNG publications.pinned,
  cắt cứng ở HORIZON_DAYS = 150
```

- `posts_per_day` (default 3) + `post_hours` (default `{9,13,19}`) + `default_hashtags` là **per-page** ⇒ khác nhau giữa các page mà không sửa code.
- **`HORIZON_DAYS=150`** — 5 tháng, dư 1 tháng an toàn dưới trần 6 tháng của FB. Vượt trần thì FB từ chối. **Có test.**
- `pinned` cho D4-A3: người ghim một bài vào slot cụ thể, allocator **không ghi đè**.

### Vì sao không có top-up lane

Ở horizon 6 tháng, page 3 post/ngày có **~540 slot** hẹn trước được. 1.000 video / 10 page = 100/page ⇒ dùng **18%**. Không thể đụng trần ⇒ **hẹn hết một lượt lúc approve**.

> Lane top-up của Rev 2 tồn tại **chỉ vì** trần 29 ngày của Reels API, và D3 đã xoá trần đó. Giữ nó lại nghĩa là cõng một lane định kỳ + alert "lane không chạy hôm nay" cho một vấn đề không còn tồn tại.

---

## 6.6 `fb.client.ts`

| Operation | Call |
|---|---|
| Publish | `POST /{page-id}/videos` — **một lời gọi**, `source` multipart stream qua `undici` |
| | `published=false` + `scheduled_publish_time` |
| | `description` = `publications.caption` |
| Reconcile | `GET /{video-id}` — đọc trạng thái thật |
| Takedown | `DELETE /{video-id}` |

**Pin version trong URL.** Pages API v18 đã tắt 26/01/2026. Lịch review 6 tháng, alert tỉ lệ lỗi publish.

Đọc file qua `storage.createReadStream()` và stream vào multipart — **không đọc cả file 15MB vào memory** rồi mới gửi.

---

## 6.7 Luồng approve → scheduled

```
approve(caption đã sửa)
  → renditions.stage = approved                        [MODULE 2 XONG]
  → distributor.pick(rendition, pages active)
  → slots.next_slot(fanpage)
  → INSERT publications (status = pending)
  → storage.createReadStream(output)
  → POST /{page-id}/videos
      ├─ lỗi  → status = pending, last_error
      │         cảnh báo reviewer: "đã approve, chưa hẹn được"
      │         make retry-publish quét lại        ← KHÔNG cần lane thường trực
      └─ ok   → fb_video_id, scheduled_at, status = scheduled
                trả về reviewer: "lên page X ngày Y giờ Z"
```

### **Lỗi FB không được làm mất approve**

Publication ở `pending`, retry được. **Reviewer không phải duyệt lại** — 2 phút người đã bỏ ra không được đốt vì một lỗi mạng.

Đây là lý do gán slot lúc approve chấp nhận được: lỗi hiện ngay trước mặt reviewer (thay vì âm thầm trong lane), nhưng nó không phá gì.

### `publications.status`

`{pending, scheduled, published, failed, cancelled}`

**`published` quay trở lại** so với Rev 2 — với `/videos` ta **đọc được** trạng thái qua `GET /{video-id}`. Rev 2 phải bỏ trạng thái này vì `video_reels` không cho read. Không còn fire-and-forget mù.

---

## 6.8 Nếu Phase 0 S2 bước 8 fail (`/videos` không vào Reels)

Chuyển sang **D3-A3**: `/videos` mặc định, `video_reels` cho video 3–90s + 9:16.

Chi phí thật, ghi ra để không ai nghĩ đây là một flag:
- **Hai code path publish**, hai loại lỗi
- **Hai horizon** (29 ngày vs 6 tháng) — `policy/slots.ts` phải biết mỗi publication dùng horizon nào
- **Top-up lane quay lại** cho đường `video_reels` + alert "lane không chạy hôm nay"
- Trạng thái `ineligible` quay lại cho video ngoài 3–90s trên đường Reels
- `DELETE` không có trên đường `video_reels` ⇒ runbook takedown tay cho một nửa số bài

Effort Phase 6: **4 ngày → ~8 ngày.**

---

## 6.9 `apps/web/` — trang cho phase này

| Component | Việc |
|---|---|
| `PageAdmin` | **Dán 1 user token → hiện danh sách page lấy về.** Badge `token_status` per page. Sửa `posts_per_day`, `post_hours`, `default_hashtags` |
| `Calendar` | Xem lịch đã hẹn per page. Ghim (`pinned`), đổi giờ, cancel |

---

## 6.10 Tests

**Thuần:** `slot-allocation` (tôn trọng `pinned`, không vượt `HORIZON_DAYS`) · `round-robin-least-loaded` (**thêm/bỏ page giữa chừng vẫn chia đều**)
**Hạ tầng:** `token-validate-rejects-short-lived` · `onboarding-exchange-flow` · `publish-error-keeps-approval` · `horizon-cap-enforced` · `module-isolation` (nạp tay `approved` → Module 3 chạy) · `secrets-never-logged` (page token + app secret)

---

## Definition of done

- [ ] Dán 1 user token ngắn hạn → tool hiện **toàn bộ** page, token đã mã hoá trong DB
- [ ] `GET /debug_token` từ chối token ngắn hạn (test, không phải thử tay)
- [ ] Approve 1 rendition → có `fb_video_id`, `scheduled_at`, reviewer thấy "page X ngày Y giờ Z"
- [ ] Bài lên đúng giờ, **kiểm mắt thấy nó ở tab Reels**
- [ ] Giả lập lỗi FB → publication `pending`, `make retry-publish` chạy được, **approve còn nguyên**
- [ ] `DELETE /{video-id}` xoá được bài đã hẹn
- [ ] Thêm/tắt page giữa chừng → phân phối tự chia lại đều
- [ ] `module-isolation` xanh
