---
type: brainstorm-summary
title: "Douyin Profile Bulk Downloader — brainstorm summary"
date: 2026-08-24
status: approved
source: IDEA.md
supersedes: "plans/260824-1128-reup-pipeline/* (deleted — no reference value)"
next: solution-design
---

# Douyin Profile Bulk Downloader — Brainstorm Summary

**Duyệt:** 2026-08-24. Toàn bộ plan `260824-1128-reup-pipeline` đã bị xoá và **không dùng làm tham khảo**. Nguồn duy nhất: `IDEA.md` + code thật trong repo.

---

## 1. Problem statement

Internal tool: dán link profile Douyin → bấm "Download all" → tải toàn bộ video tab "Đã đăng" về đĩa, có tiến độ, resume được sau crash, chạy lại chỉ tải phần mới. Cookie do Chrome extension (MV3) tự thu và push xuống backend — không paste tay.

**Downloader là microservice độc lập.** Dubbing / Facebook publish là service khác, **không couple**: không import chéo, không HTTP call chéo, không chung DB. Consumer tương lai đọc qua REST API + file trên đĩa.

### Out of scope (v1)
Tab Thích / Sưu tầm / collection / live · TikTok · multi-tenant (1 API key dùng chung) · transcode / thumbnail / subtitle.

---

## 2. Trạng thái repo (đọc từ code, không từ docs)

| Hạng mục | Trạng thái |
|---|---|
| Parser fork submodule `src/Douyin_TikTok_Download_API` @ `5fe4e4d` | ✅ có |
| `cookie_provider.py` — đọc Redis `douyin:cookie:current` mỗi request, async, client cache theo event loop, `socket_timeout=1`, fallback env → `config.yaml` | ✅ có |
| `update_cookie()` upstream (ghi cookie vào file tracked + print stdout 3×) | ✅ đã xoá hẳn |
| CI: gitleaks + rule `douyin-session-cookie` + canary, `check-patch-size.sh` | ✅ có |
| ~20 case pytest trong fork | ✅ có |
| Orchestrator API · worker · DB · frontend · extension · compose | ❌ chưa có gì |

**Kết luận:** lớp cookie xong, còn lại greenfield.

### Endpoint parser đã verify bằng code (prefix `/api/douyin/web/`)

| Route | Dùng cho |
|---|---|
| `fetch_user_post_videos?sec_user_id&max_cursor&count` | Enumerate. Trả raw `aweme/v1/web/aweme/post` — **`aweme_list[].video.play_addr.url_list` có sẵn** |
| `fetch_one_video?aweme_id` | Re-resolve URL hết hạn |
| `get_sec_user_id?url` | Short link → `sec_user_id`. **IDEA.md §3.3 không cần code** |
| `handler_user_profile?sec_user_id` | Nickname |

---

## 3. Quyết định đã chốt

| # | Quyết định | Lý do / hệ quả |
|---|---|---|
| D1 | **TypeScript + NestJS** cho API + worker | Team quen TS. **Hệ quả đã chấp nhận:** parser là Python ⇒ buộc phải là service HTTP riêng, không import được. "1 process" thực tế = 4 container (nest · parser · postgres · redis). Chọn Python thì còn 3 |
| D2 | **API + worker chung 1 process** | Đủ cho 1–3 người dùng. Tách process sau: đổi 0 dòng logic nhờ D5 |
| D3 | **Postgres** | Cần `FOR UPDATE SKIP LOCKED` cho 4–8 luồng download claim video an toàn |
| D4 | **Đĩa local**, `${DATA_DIR}/douyin/{sec_user_id}/{aweme_id}.mp4` | Không MinIO. Bọc sau interface `Storage` để đổi sau |
| D5 | **DB làm queue**, không broker | Xem §4 |
| D6 | **Polling 1s**, không SSE ở v1 | Job chạy hàng giờ, trễ 1s không ai cảm nhận. Reconnect miễn phí |
| D7 | **Monorepo `services/`** | `services/downloader`, sau này `services/dubbing`… Deploy độc lập. Không couple code, có couple CI |
| D8 | Auth: header `X-Api-Key` so với env | Internal tool, đủ |

---

## 4. Cơ chế chạy job — approaches đã cân

| | Cách | Kết luận |
|---|---|---|
| **A1** | **DB làm queue.** `jobs`/`videos` là chân lý. Lane poll `FOR UPDATE SKIP LOCKED` | ✅ **CHỌN.** Resume là hệ quả tự nhiên, không phải tính năng thêm. Một nguồn chân lý. Tách worker ra process riêng không đổi logic |
| A2 | BullMQ trên Redis đã có | ❌ Thành **hai nguồn chân lý** (job Redis + row Postgres). Lệch nhau sau crash là class bug khó nhất của loại hệ này. Trả rủi ro để mua thứ Postgres đã cho |
| A3 | Queue in-memory | ❌ Mất hết khi restart ⇒ vi phạm tiêu chí nghiệm thu |

**yt-dlp cho pha download:** loại. Có `play_addr` rồi thì tải chỉ là HTTP GET + `Referer` — yt-dlp không thêm giá trị, thêm dependency hay vỡ.

---

## 5. Ba sửa đổi so với IDEA.md (đã duyệt)

### F1 — `play_addr` PHẢI cache. §3.2 của IDEA.md mâu thuẫn §3.1 và sẽ gây ban

§3.2: *"không cache URL, re-resolve trước mỗi lần tải"*. §3.1: API enumerate siết ~100–150 req/giờ/IP. Nhưng `fetch_one_video` **đi cùng đường risk-control**.

Profile 500 video:

| | Theo §3.2 | Đã duyệt |
|---|---|---|
| Enumerate | 25 req | 25 req |
| Resolve URL | **500 req** | ~0 req |
| Tổng | **525 req** — vượt 3–5× hạn mức/giờ, ban gần như chắc | **25 req** |

Enumerate đã trả `play_addr` sẵn (verify bằng code).

**Cách làm:** lưu `play_addr` + `url_expires_at` (lấy lúc nào + TTL 2h). Download: URL tươi → dùng luôn; hết hạn **hoặc** CDN trả 403/410 → `fetch_one_video` 1 lần rồi retry. Xử lý **phản ứng** thay vì phòng ngừa mù. Vẫn không tin URL cũ.

### F2 — Enumerate lane concurrency = 1 **toàn hệ thống**

Rate limit theo IP, không theo job. IDEA.md nói "tuần tự" nhưng không nói phạm vi. 3 job song song = 3× tần suất = ban. Job thứ 2 xếp hàng chờ. Download vẫn 4–8 luồng (CDN không siết).

### F3 — Thêm `lease_expires_at`, nếu không thì tiêu chí "kill worker → resume" KHÔNG đạt

Crash giữa download để lại video kẹt `status=downloading` vĩnh viễn, không ai nhặt. IDEA.md không có cơ chế này.

Worker claim → set lease +5 phút + heartbeat. Reaper mỗi phút: `downloading` + lease hết hạn → về `pending`.

---

## 6. Data model

Delta so với IDEA.md §5.1 được đánh dấu.

```
profiles   sec_user_id PK · nickname · last_synced_at

jobs       id UUID PK · sec_user_id → profiles · status
           total_videos · enumerated_count · downloaded_count · failed_count
           max_cursor          -- checkpoint mỗi trang enumerate
           error · created_at · updated_at

videos     aweme_id PK         -- dedupe toàn cục ⇒ incremental miễn phí
           sec_user_id · desc · create_time · duration_ms
           play_addr           -- (F1) MỚI
           url_expires_at      -- (F1) MỚI
           storage_path · status · attempts · last_error
           lease_expires_at    -- (F3) MỚI
```

`jobs.status`: `pending|enumerating|downloading|completed|failed|blocked|cancelled`
`videos.status`: `pending|downloading|done|failed|skipped`

`aweme_id` là PK toàn cục ⇒ chạy lại profile tự động incremental, **không cần logic thêm**.

---

## 7. Lane

**Enumerate** — global 1 luồng:
```
cursor = job.max_cursor or 0
loop:
  page = GET fetch_user_post_videos(sec_user_id, cursor, count=20)
  upsert videos ON CONFLICT (aweme_id) DO NOTHING   -- kèm play_addr + url_expires_at
  job.enumerated_count += len(page.aweme_list)
  job.max_cursor = page.max_cursor                  -- checkpoint MỖI TRANG
  if not page.has_more: break
  sleep(3–5s + jitter)
job.total_videos = count(videos where sec_user_id = …)
```
Phát hiện bị chặn: `aweme_list` rỗng nhưng `has_more=1`, **hoặc** 3 lỗi parser liên tiếp → `status=blocked`, dừng, **giữ nguyên checkpoint**.

**Download** — 4–8 luồng:
```
1. claim 1 video status=pending  (FOR UPDATE SKIP LOCKED) + set lease
2. url tươi → dùng; hết hạn → fetch_one_video
3. GET file, header Referer=https://www.douyin.com/ + User-Agent khớp cookie
4. verify content-length
5. ghi .tmp → fs.rename (atomic) → status=done, storage_path
```
Retry 3 lần exponential backoff → `status=failed`, ghi `last_error`, **đi tiếp, không kill cả job**.

---

## 8. Cookie — hợp đồng đã cố định bởi code có sẵn

```
Extension MV3 ──POST /internal/cookie (X-Api-Key)──> NestJS
                                                       │ ghi Redis
                                                       ▼
                              "douyin:cookie:current"  TTL 24h
                                                       │
                                       Parser đọc mỗi request  ✅ ĐÃ XONG
```

- Extension dùng `chrome.cookies` (không phải `document.cookie`) vì `sessionid_ss` / `passport_csrf_token` là HttpOnly. Đây là **toàn bộ lý do extension tồn tại**.
- `chrome.alarms` mỗi 20 phút + push lúc `onInstalled`.
- Options page: backend URL + API key vào `chrome.storage.local`, thêm nút "Push now" + kết quả push gần nhất.
- Key phụ `douyin:cookie:pushed_at` để `/internal/cookie/status` tính tuổi — **không suy ra từ TTL** (TTL refresh mỗi lần push nên không nói gì về tuổi).
- Cookie không bao giờ vào log, error message, hay API response.

---

## 9. Endpoints

```
POST   /api/jobs                            { url } -> { job_id }
GET    /api/jobs                            list, paginate
GET    /api/jobs/{id}                       detail + counters
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/retry                 chỉ video status=failed
GET    /api/profiles/{sec_user_id}/videos   paginate, browse kết quả

POST   /internal/cookie                     { platform, cookie }  <- extension
GET    /internal/cookie/status              { present, age_seconds, last_ok_at }  (KHÔNG trả cookie)
```

Không có `/events` SSE ở v1 (D6).

---

## 10. Frontend

React + Tailwind, một trang:
- Input URL + nút "Download all"
- List job đang chạy / đã xong
- Mỗi job **hai thanh riêng biệt** — enumerate ("Đang quét: 340 video", indeterminate tới khi `has_more=0`) và download ("127 / 340"). **Không gộp thành một %**: trước khi enumerate xong thì mẫu số chưa tồn tại.
- Badge cookie: xanh <30′, vàng cũ hơn, đỏ không có. Thứ đầu tiên cần nhìn khi job fail.
- Cancel / retry failed
- Polling 1s

---

## 11. Milestones

| M | Nội dung | Xong khi |
|---|---|---|
| **M1** | compose 4 container · schema + migration · lane enumerate + download · cookie paste tay | Tải hết 1 profile <50 video, số file khớp `downloaded_count` |
| **M2** | lease + reaper · incremental · retry per-video · phát hiện `blocked` · rate limit đúng từng pha | Kill worker giữa job → restart → tiếp tục, không tải lại file đã xong |
| **M3** | Extension MV3 auto refresh, bỏ ô paste cookie khỏi UI | Không ai chạm cookie bằng tay |
| **M4** | Browse video đã tải · polish UI · preflight dung lượng đĩa | |

M1 chứng minh luồng chạy thật. Extension chỉ thay đúng cái ô text đó, interface không đổi.

---

## 12. Tiêu chí nghiệm thu

- [ ] Đổi cookie trong Redis → parser dùng cookie mới ở request kế tiếp, **không restart**
- [ ] Job profile 200+ video hoàn tất, số file trong storage khớp `downloaded_count`
- [ ] Kill worker giữa pha download → restart → tiếp tục, **không tải lại file đã xong**
- [ ] Chạy lại cùng profile lần 2 → **chỉ tải video mới**
- [ ] Cookie hết hạn giữa job → `blocked` (không phải `failed`) → push cookie mới → retry chạy tiếp từ checkpoint
- [ ] Cookie không xuất hiện trong bất kỳ log / API response nào
- [ ] `docker compose up` từ máy sạch chạy được
- [ ] Parser **không** expose ra ngoài internal network (không có `ports:` trong compose)
- [ ] Enumerate 2 job song song → tổng tần suất request vẫn = 1 luồng (F2)

---

## 13. Risks

| Risk | Xử lý |
|---|---|
| Cookie bị ban | `blocked` + giữ checkpoint, không `failed`. Badge cookie trên UI. Dev dùng tài khoản phụ khi test |
| Douyin đổi `a_bogus` | Pull upstream định kỳ theo `PATCH_NOTES.md`. Alert tỉ lệ job `blocked` |
| Upstream refactor hoisting header thành module-level constant | Cookie đóng băng lúc process start, **không có exception**. `PATCH_NOTES.md` §2 đã ghi. Watch cho spike job `blocked`, không watch exception |
| Upstream trả `update_cookie()` về sau merge | `tests/test_web_crawler_security.py` fail CI |
| IP Việt Nam bị chặn | Proxy trong `crawlers/douyin/web/config.yaml`. Cần throughput hơn: nhiều instance parser sau proxy khác nhau, không dồn 1 endpoint |
| Đầy đĩa | Preflight trước job lớn (~5–30MB/video, 1000 video ≈ 10–20GB) + alert ngưỡng |
| Video kẹt `downloading` sau crash | F3 lease + reaper |
| URL hết hạn giữa job dài | F1 reactive re-resolve |

### Guard phải có mutation test (rút từ Phase 1 false-greens)

"Suite xanh" **không phải** bằng chứng invariant được giữ. Mỗi guard dưới đây phải được **cố tình phá** và bắt test đỏ:

| Invariant | Mutation phải làm test đỏ |
|---|---|
| Lease reaper nhặt lại video kẹt | Bỏ reaper → test resume phải đỏ |
| Cookie không vào log | Thêm `log(cookie)` trên đường header → test phải đỏ |
| Enumerate global 1 luồng | Đổi thành per-job → test tần suất phải đỏ |
| `blocked` ≠ `failed` | Map `blocked` → `failed` → test phải đỏ |
| Atomic file write | Bỏ `.tmp` + rename → test crash-giữa-ghi phải đỏ |

Và verify rằng mutation **thật sự match** — hai lần ở Phase 1 `sed` mutation match rỗng, nên "suite vẫn xanh" là mutation tồi chứ không phải gap thật.

---

## 14. Unresolved — không chặn code

1. `DATA_DIR` mount ở đâu trên máy thật, quota bao nhiêu.
2. Có cần preview video trong UI (cần serve static mp4 / presigned) hay chỉ cần đường dẫn file? → quyết ở M4.
3. Ngưỡng alert đĩa cụ thể.
4. Lịch pull upstream: hàng tuần thủ công hay CI job kiểm.

---

## 15. Next step

`/solution-design` — chốt kiến trúc chi tiết (workflow diagram, module boundary, side-effect) trước khi `/plan` sinh phase.
