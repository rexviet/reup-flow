# IDEA.md — Douyin Profile Bulk Downloader

Spec bàn giao cho coding agent. Đọc hết phần "Bối cảnh" và "Ràng buộc quan trọng" trước khi
viết dòng code đầu tiên — có vài giả định sai phổ biến được nêu rõ ở đó.

---

## 1. Mục tiêu

Xây một internal tool: nhập link profile Douyin vào web UI → bấm "Download all" → hệ thống tải
toàn bộ video của profile đó về object storage, hiển thị tiến độ real-time.

Cookie Douyin được một Chrome extension tự động thu thập và đẩy xuống backend, không phải
paste tay.

### Phạm vi (in scope)

- Tải toàn bộ video ở tab "Đã đăng" (post) của một profile
- Job chạy nền, có tiến độ, resume được sau khi crash
- Incremental: chạy lại cùng một profile chỉ tải phần mới
- Chrome extension (MV3) tự refresh cookie

### Ngoài phạm vi (out of scope, v1)

- Tab "Thích" / "Sưu tầm" / collection / live
- TikTok (repo upstream có hỗ trợ, nhưng v1 chỉ làm Douyin)
- Multi-tenant / phân quyền người dùng — đây là internal tool, một API key dùng chung là đủ
- Transcode, thumbnail generation, subtitle

---

## 2. Bối cảnh & quyết định kiến trúc

### 2.1 Upstream repo

Fork `https://github.com/Evil0ctal/Douyin_TikTok_Download_API`.

**Điểm mấu chốt phải hiểu:** repo này là một **parser stateless**, không phải download manager.
Nó nhận URL → trả JSON metadata (bao gồm `play_addr` CDN URL). Nó không có job queue, không có
state, không có progress tracking. **Không được** nhét job queue vào trong nó. Coi nó như một
internal HTTP dependency, và viết lớp orchestrator riêng ở trên.

### 2.2 Kiến trúc tổng thể

```
Chrome extension ──┐
                   ├──> Orchestrator API ──┬──> Cookie store (Redis)
Frontend web UI ───┘        (FastAPI)      │
                                            └──> Job worker ──> Parser service (fork)
                                                       │              (stateless)
                                                       └──> Object storage (MinIO/S3)
```

Tất cả chạy bằng `docker-compose`. Parser service **không expose ra ngoài** — chỉ orchestrator
và worker gọi được, qua internal network.

### 2.3 Vì sao phải patch parser

Upstream đọc cookie từ `crawlers/douyin/web/config.yaml` **một lần lúc khởi động**. Extension
push cookie mới mỗi ~20 phút, nên nếu giữ nguyên hành vi này thì phải `docker restart` liên tục
— làm rớt request đang chạy.

→ Patch để cookie được đọc từ Redis **theo từng request**. Đây là điều kiện tiên quyết cho toàn
bộ phần tự động hoá.

---

## 3. Ràng buộc quan trọng (đọc kỹ, đây là chỗ dễ làm sai)

### 3.1 Hai pha có rate limit hoàn toàn khác nhau

| Pha | Gọi gì | Rate limit | Song song |
|---|---|---|---|
| **Enumerate** | API `aweme/v1/web/aweme/post` qua parser | Nghiêm ngặt. ~100–150 req/giờ/IP | Không. Tuần tự, sleep 3–5s/trang |
| **Download** | File trên CDN (`*.douyinvod.com`) | Thoáng, không chịu risk control như API | Có. 4–8 luồng |

Đừng áp cùng một rate limit cho cả hai pha — sẽ chậm hơn cần thiết cả chục lần.

Lưu ý enumerate rẻ hơn nhiều so với cảm giác ban đầu: profile 500 video với `count=20` chỉ tốn
~25 request.

### 3.2 `play_addr` URL hết hạn

CDN URL có expiry (thường vài giờ). **Không cache URL vào DB.** Lưu `aweme_id` làm nguồn chân
lý, re-resolve URL ngay trước khi tải. Nếu một download activity fail vì URL hết hạn, retry
phải bắt đầu bằng việc resolve lại.

### 3.3 `sec_user_id`, không phải username

Input người dùng là URL dạng `https://www.douyin.com/user/MS4wLjABAAAA...`. Phần
`MS4wLjABAAAA...` là `sec_user_id`, đây mới là thứ API cần. Cũng phải xử lý được short link
`https://v.douyin.com/xxxxx/` (follow redirect để lấy URL đầy đủ).

### 3.4 Cookie là credential

- Không log ra stdout, không ghi vào error message, không trả về trong API response
- Redis key có TTL (24h)
- Endpoint nhận cookie phải có auth

### 3.5 Cookie sẽ bị ban

Chuyện bình thường với loại tool này. Hệ thống phải xử lý gracefully: phát hiện response
bất thường (empty `aweme_list` nhưng `has_more=1`, hoặc status code lạ) → đánh dấu job là
`blocked` chứ không phải `failed`, giữ nguyên progress để resume khi có cookie mới.

Dev note: dùng tài khoản Douyin phụ khi test.

---

## 4. Component 1 — Parser service (fork + patch)

### 4.1 Việc cần làm

Fork repo, tạo branch `feat/redis-cookie`, giữ patch **càng nhỏ càng tốt** để còn merge upstream
về sau (upstream update thuật toán `a_bogus` thường xuyên, bắt buộc phải pull được).

### 4.2 Các bước

1. **Khảo sát trước.** Tìm chỗ cookie được load. Điểm bắt đầu:
   - `crawlers/douyin/web/config.yaml` — key `Cookie:`
   - Grep `Cookie` trong `crawlers/douyin/web/` để tìm module đọc config
   - Xác định xem cookie đang là module-level constant hay được đọc lại mỗi request

2. **Kiểm tra xem có đường tắt không.** Một số version của các endpoint nhận `cookie` làm
   request parameter. Nếu có, dùng luôn, **bỏ qua bước 3** và ghi lại phát hiện này vào README
   của fork.

3. **Patch.** Thay chỗ đọc config bằng một hàm resolver:

   ```python
   # crawlers/douyin/web/cookie_provider.py  (file mới)
   import os, redis

   _r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
   _KEY = "douyin:cookie:current"

   def get_cookie() -> str:
       cookie = _r.get(_KEY)
       if cookie:
           return cookie
       return _fallback_from_yaml()   # giữ config.yaml làm fallback
   ```

   Nguyên tắc: **gọi `get_cookie()` tại thời điểm build request**, không phải lúc import module.
   Đây chính là toàn bộ mục đích của patch.

4. **Giữ fallback.** Nếu Redis không có key hoặc không kết nối được → dùng `config.yaml`. Như
   vậy fork vẫn chạy độc lập được, và test local không cần Redis.

5. **Ghi lại patch.** Tạo `PATCH_NOTES.md` trong fork liệt kê chính xác file nào bị sửa và
   sửa gì. Khi merge upstream, đây là checklist.

### 4.3 Tiêu chí hoàn thành

- Đổi giá trị key Redis → request tiếp theo dùng cookie mới, **không cần restart**
- Xoá key Redis → service vẫn chạy bằng cookie trong `config.yaml`
- Redis down → service không crash, log warning, dùng fallback
- `git diff upstream/main` không quá ~50 dòng thay đổi

---

## 5. Component 2 — Orchestrator API

FastAPI. Đây là service duy nhất frontend và extension nói chuyện trực tiếp.

### 5.1 Data model

```
profiles
  sec_user_id      TEXT PRIMARY KEY
  nickname         TEXT
  last_synced_at   TIMESTAMPTZ

jobs
  id               UUID PRIMARY KEY
  sec_user_id      TEXT REFERENCES profiles
  status           TEXT   -- pending|enumerating|downloading|completed|failed|blocked|cancelled
  total_videos     INT    -- NULL cho đến khi enumerate xong
  enumerated_count INT
  downloaded_count INT
  failed_count     INT
  max_cursor       BIGINT -- checkpoint để resume pha enumerate
  error            TEXT
  created_at, updated_at

videos
  aweme_id         TEXT PRIMARY KEY      -- dedupe key toàn cục
  sec_user_id      TEXT
  desc             TEXT
  create_time      TIMESTAMPTZ
  duration_ms      INT
  storage_key      TEXT                  -- NULL nếu chưa tải
  status           TEXT   -- pending|downloading|done|failed|skipped
  attempts         INT
  last_error       TEXT
```

`aweme_id` là primary key toàn cục → chạy lại profile tự động incremental, không cần logic gì
thêm.

### 5.2 Endpoints

```
POST   /api/jobs                  { "url": "..." }  -> { job_id }
GET    /api/jobs                  danh sách, phân trang
GET    /api/jobs/{id}             chi tiết + counters
GET    /api/jobs/{id}/events      SSE stream tiến độ
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/retry       chỉ retry các video status=failed

GET    /api/profiles/{sec_user_id}/videos   phân trang, để browse kết quả

POST   /internal/cookie           { platform, cookie }   <- extension gọi
GET    /internal/cookie/status    { present, age_seconds, last_ok_at }  (không trả cookie)
```

Auth: header `X-Api-Key`, so với env var. Đủ cho internal tool.

### 5.3 SSE

Payload mỗi ~1s hoặc khi có thay đổi:

```json
{ "status": "downloading", "total": 340, "enumerated": 340,
  "downloaded": 127, "failed": 2, "current": "7412…" }
```

Frontend hiển thị **hai thanh riêng biệt** — xem mục 7.

---

## 6. Component 3 — Job worker

### 6.1 Lựa chọn công nghệ

Mặc định: **BullMQ hoặc Celery**. Đủ dùng, dựng nhanh.

Cân nhắc **Temporal** nếu hạ tầng Temporal đã có sẵn — nó fit khá đẹp vì job chạy hàng giờ,
cần durable timer cho rate limit, và có thể dùng signal để bơm cookie mới vào workflow đang
chạy thay vì fail cả job. Nhưng **đừng dựng cluster Temporal mới chỉ vì tool này.**

### 6.2 Pha enumerate

```
cursor = job.max_cursor or 0
loop:
    page = parser.fetch_user_post(sec_user_id, cursor, count=20)
    upsert videos (ON CONFLICT aweme_id DO NOTHING)
    job.enumerated_count += len(page.aweme_list)
    job.max_cursor = page.max_cursor        # checkpoint mỗi trang
    if not page.has_more: break
    sleep(3–5s + jitter)
job.total_videos = count(videos where sec_user_id = ...)
```

Checkpoint `max_cursor` sau **mỗi trang** — crash giữa chừng thì resume từ đó.

Phát hiện bị chặn: `aweme_list` rỗng nhưng `has_more = 1`, hoặc parser trả lỗi liên tiếp 3 lần
→ `status = blocked`, dừng, giữ nguyên checkpoint.

### 6.3 Pha download

Song song 4–8 worker, mỗi worker:

```
1. lấy 1 video status=pending
2. resolve play_addr qua parser  (KHÔNG dùng URL cache)
3. tải về temp file, header: Referer=https://www.douyin.com/ + User-Agent khớp cookie
4. verify content-length, đẩy lên object storage
5. cập nhật storage_key + status=done
```

- Retry: 3 lần, exponential backoff. Quá 3 lần → `status=failed`, ghi `last_error`, đi tiếp,
  **không kill cả job**
- Tải vào temp file rồi mới move/upload — tránh file hỏng nửa chừng
- Storage key: `douyin/{sec_user_id}/{aweme_id}.mp4`

---

## 7. Component 4 — Frontend

React + Tailwind. Một trang là đủ.

- Ô input URL profile + nút "Download all"
- Danh sách job đang chạy và đã xong
- Mỗi job: **hai thanh tiến độ riêng**
  - Thanh 1 — enumerate: "Đang quét: 340 video" (indeterminate cho đến khi `has_more=0`)
  - Thanh 2 — download: "127 / 340"

  Không gộp thành một con số % duy nhất — trước khi enumerate xong thì mẫu số chưa tồn tại,
  ghép vào sẽ ra progress bar nhảy lung tung.
- Badge trạng thái cookie (lấy từ `/internal/cookie/status`): xanh nếu tuổi < 30 phút, vàng nếu
  cũ hơn, đỏ nếu không có. Đây là thứ đầu tiên cần nhìn khi job fail.
- Nút cancel / retry failed
- Kết nối SSE, có auto-reconnect

---

## 8. Component 5 — Chrome extension (MV3)

### 8.1 Vì sao cần

`document.cookie` **không** lấy được cookie HttpOnly (`sessionid_ss`, `passport_csrf_token`) —
đây thường là lý do request fail dù cookie trông có vẻ đầy đủ. `chrome.cookies` API thì lấy
được hết. Đó là toàn bộ lý do tồn tại của extension này.

### 8.2 Manifest

```json
{
  "manifest_version": 3,
  "permissions": ["cookies", "alarms", "storage"],
  "host_permissions": ["*://*.douyin.com/*"],
  "background": { "service_worker": "sw.js" },
  "options_page": "options.html"
}
```

### 8.3 Service worker

```js
async function pushCookies() {
  const cookies = await chrome.cookies.getAll({ domain: '.douyin.com' });
  if (!cookies.length) return;
  const header = cookies.map(c => `${c.name}=${c.value}`).join('; ');
  const { backend, apiKey } = await chrome.storage.local.get(['backend', 'apiKey']);
  await fetch(`${backend}/internal/cookie`, {
    method: 'POST',
    headers: { 'X-Api-Key': apiKey, 'Content-Type': 'application/json' },
    body: JSON.stringify({ platform: 'douyin', cookie: header })
  });
}

chrome.alarms.create('refresh', { periodInMinutes: 20 });
chrome.alarms.onAlarm.addListener(pushCookies);
chrome.runtime.onInstalled.addListener(pushCookies);
```

### 8.4 Options page

Hai field: backend URL, API key. Lưu vào `chrome.storage.local`. Thêm nút "Push now" + hiển thị
kết quả lần push gần nhất để debug.

---

## 9. Lộ trình

Làm theo thứ tự. Mỗi milestone phải chạy được end-to-end trước khi sang cái tiếp theo.

**M1 — Xương sống (ưu tiên cao nhất)**
Fork + patch Redis cookie. Orchestrator với endpoint tạo job. Worker enumerate + download.
Cookie **paste tay** qua một ô text tạm trên UI. Tải xong một profile nhỏ (< 50 video).

Mục tiêu M1 là chứng minh luồng chạy thật. Extension chỉ thay thế đúng cái ô text đó về sau,
interface không đổi.

**M2 — Độ bền**
Resume sau crash. Incremental (chạy lại chỉ tải phần mới). Retry per-video. Phát hiện trạng
thái `blocked`. Rate limit đúng cho từng pha.

**M3 — Extension**
MV3 + auto refresh. Bỏ ô paste cookie khỏi UI.

**M4 — Hoàn thiện**
SSE progress mượt. Browse video đã tải. Cancel/retry. Badge trạng thái cookie.

---

## 10. Tiêu chí nghiệm thu

- [ ] Đổi cookie trong Redis → parser dùng cookie mới ở request kế tiếp, không restart
- [ ] Job trên profile 200+ video hoàn tất, số file trong storage khớp `downloaded_count`
- [ ] Kill worker giữa pha download → restart → tiếp tục, không tải lại file đã xong
- [ ] Chạy lại cùng profile lần 2 → chỉ tải video mới
- [ ] Cookie hết hạn giữa job → job thành `blocked` (không phải `failed`), push cookie mới →
      retry chạy tiếp từ checkpoint
- [ ] Cookie không xuất hiện trong bất kỳ log hay API response nào
- [ ] `docker compose up` từ máy sạch chạy được
- [ ] Parser service không expose ra ngoài internal network

---

## 11. Ghi chú vận hành

- **Pull upstream định kỳ.** Thuật toán `a_bogus` trong `crawlers/douyin/web/abogus.py` phải
  chạy theo Douyin. Upstream fix nhanh nhưng phải chủ động pull. Đặt lịch kiểm tra hàng tuần.
- **Proxy.** IP Việt Nam đôi khi bị Douyin chặn thẳng. Cấu hình proxy nằm trong cùng file
  `crawlers/douyin/web/config.yaml`. Nếu cần throughput cao hơn, chạy nhiều instance parser sau
  các proxy khác nhau thay vì dồn tải vào một endpoint.
- **Dung lượng đĩa.** Video Douyin ~5–30MB/cái. Profile 1000 video ≈ 10–20GB. Kiểm tra chỗ
  trống trước khi chạy job lớn.
- **Pháp lý.** ToS Douyin cấm crawl tự động, và video thuộc bản quyền của creator. Tool này
  dùng cho nội dung mà tổ chức sở hữu hoặc đã có thoả thuận với creator.
