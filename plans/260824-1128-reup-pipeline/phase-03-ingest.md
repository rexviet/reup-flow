---
phase: 3
title: Module 1 — Ingest (M1)
status: not_started
effort: 3 ngày
blockedBy: [2]
---

# Phase 3 — Module 1: Ingest

Douyin → mp4 gốc + **caption gốc** + số engagement, trên ổ đĩa.

Kết thúc phase này: **tải xong 1 talent, có caption gốc và số engagement.** Cookie còn dán tay.

---

## 3.1 Hai lane, hai bài toán khác nhau

| Lane | Concurrency | Vì sao con số đó |
|---|---|---|
| `enumerate.lane.ts` | **1**, rate-limited | Rate limit Douyin ~100–150 req/giờ/IP. Đây là tài nguyên khan, phải đi tuần tự |
| `download.lane.ts` | **8** | CDN không rate-limit như API. Đây là băng thông, không phải quota |

**Đừng gộp hai lane này.** Chúng có hai loại giới hạn khác nhau, và gộp lại nghĩa là download bị kéo xuống tốc độ enumerate.

---

## 3.2 `enumerate.lane.ts`

Đọc `jobs` (một job = một `sec_user_id`), phân trang `aweme_list[]`, upsert vào `videos`.

### Lưu gì từ `aweme_list[]` — **cả ba nhóm, miễn phí trong cùng một response**

| Nhóm | Cột | Dùng để |
|---|---|---|
| Caption gốc | `description` ← `desc` **thô, nguyên văn** | Module 2 dịch |
| Curation | `stat_play`, `stat_digg`, `stat_comment`, `stat_share` | Rank chọn video đáng dub |
| Kỹ thuật | `duration_ms`, `width`, `height`, `fps`, `create_time` | Render conform, bỏ `prepare.lane` |
| URL | `play_url` + `url_fetched_at` | Cache TTL 2h |

**Không xử lý `desc` ở đây.** Làm sạch là việc của Module 2. Nếu Module 1 làm sạch sẵn thì đổi luật làm sạch phải **tải lại từ Douyin** — mà đó là tài nguyên khan nhất trong hệ thống.

### `play_url` cache TTL 2h, không re-resolve từng video

`IDEA.md §3.2` đọc theo nghĩa đen là re-resolve từng video — tốn ~1 lời gọi API/video, vượt budget giờ. Response enumerate **đã chứa `play_addr`**. Dùng nó, cache 2h (`URL_TTL_SECONDS`), re-resolve chỉ khi download gặp 403.

### `policy/blocked.ts` — phân biệt "hết dữ liệu" với "bị ban"

Hai điều kiện, cái nào cũng đủ để chuyển job sang `blocked`:
- `aweme_list` **rỗng** nhưng `has_more = 1` — dấu hiệu điển hình của cookie bị ban
- 3 lỗi liên tiếp

**`blocked` phải giữ `max_cursor`.** Job resume được sau khi cookie mới được đẩy vào, không phải chạy lại từ đầu. Đây là toàn bộ lý do `jobs` có cột checkpoint.

`blocked` **không** tăng `attempts` — nó không phải lỗi transient của một request, nó là trạng thái của credential.

---

## 3.3 `download.lane.ts`

Thứ tự bắt buộc — mỗi bước là một failure mode đã biết:

```
1. GET play_url (stream)
2. ghi vào /data/source/{sec_user_id}/{aweme_id}.mp4.tmp
3. verify content-length khớp bytes đã ghi
4. fs.rename(.tmp → .mp4)          ← atomic trên cùng filesystem
5. commit transaction: stage = downloaded, storage_key
```

- **Bước 3 không bỏ được.** Download đứt giữa đường cho ra file mp4 hợp lệ về cú pháp nhưng thiếu đuôi, và ffmpeg sẽ chấp nhận nó — lỗi chỉ lộ ra ở video đã đăng.
- **Bước 4 trước bước 5**, không ngược lại. Đây là quy tắc `advance()` atomic của Phase 2.
- `play_url` 403 → re-resolve URL, **không** tăng `attempts` ngay (URL hết hạn là chuyện thường, không phải lỗi).

---

## 3.4 `policy/` — hàm thuần, test không cần DB

| File | Việc | Case dễ sai |
|---|---|---|
| `url-parse.ts` | Trích `sec_user_id` từ URL profile | **Follow short link `v.douyin.com`** — nó là redirect, không phải URL chứa id |
| `pagination.ts` | `max_cursor` → request kế | `has_more = 0` vs `aweme_list` rỗng là hai chuyện khác nhau |
| `blocked.ts` | Xem §3.2 | |
| `retry.ts` | Backoff | Rate-limit (429) phải backoff **dài hơn** lỗi mạng |

---

## 3.5 `parser.client.ts` + `cookie.store.ts`

- `parser.client.ts` gọi `PARSER_BASE_URL`. Parser **không có port ra ngoài** — chỉ `api`/`worker` gọi được.
- `cookie.store.ts` ghi `douyin:cookie:current` (TTL 24h) + `douyin:cookie:meta` (`pushed_at`, `last_ok_at`, `sha256` — **không chứa giá trị**).
- `POST /internal/cookie` (có `API_KEY` guard) để dán cookie tay ở phase này. Extension tự động ở Phase 7.

`sha256` trong meta cho phép UI hiện "cookie đã đổi" mà không bao giờ đọc giá trị. Đây là cách health badge biết cookie tươi mà không rò secret ra frontend.

---

## 3.6 API endpoints (`apps/api/`)

`api-key.guard.ts` — `crypto.timingSafeEqual`, **áp global** (không phải per-route; một route quên guard là một route hở).

| Route | Việc |
|---|---|
| `POST /api/talents` | Thêm talent (URL profile → `sec_user_id`) |
| `POST /api/jobs` | Tạo job enumerate cho một talent |
| `GET /api/jobs/:id` | Trạng thái + `enumerated_count` + `blocked_reason` |
| `POST /internal/cookie` | Đẩy cookie |
| `GET /api/videos` | Filter theo talent / stage, sort theo `stat_*` |

---

## 3.7 Tests

**Thuần:** `url-parse` (kể cả short link) · `blocked-detection` · `retry-policy` · `pagination`
**Hạ tầng:** `download-verifies-content-length` · `download-403-reresolves-without-burning-attempts` · `blocked-keeps-checkpoint` · `enumerate-respects-rate-limit`

---

## Definition of done

- [ ] Chạy 1 talent thật → `videos` có hàng với **`description` không rỗng** và `stat_*` có số
- [ ] mp4 nằm ở `/data/source/{sec_user_id}/{aweme_id}.mp4`, mở được bằng ffprobe
- [ ] Kill worker giữa download → file `.tmp` bị bỏ lại, hàng DB **không** ở `downloaded`, retry chạy lại sạch
- [ ] Cookie sai → job `blocked` với `blocked_reason`, `max_cursor` còn nguyên, `attempts` không tăng
- [ ] Đẩy cookie mới → job resume từ `max_cursor`, không tải lại từ đầu
