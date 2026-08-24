---
phase: 7
title: Automation & Ops (M5)
status: not_started
effort: 3 ngày
blockedBy: [6]
---

# Phase 7 — Automation & Ops

Kết thúc phase này: **chạy không cần trông.**

Mọi thứ ở đây đều là thứ mà thiếu nó thì hệ thống vẫn chạy — cho tới lúc nó không chạy và **không ai biết**.

---

## 7.1 Extension cookie (`apps/extension/`) — MV3

### Không dùng trực tiếp `chrome-cookie-sniffer/` của upstream

Fork **đã có** extension này (MV3, `permissions: [webRequest, storage, activeTab, cookies]`). Nhưng nó **trigger theo `webRequest`** — tức chỉ refresh khi **có người mở Douyin**. `IDEA.md §8` muốn cookie tươi **không phụ thuộc hành vi duyệt web**.

### Viết mới, lấy lại hai ý hay của nó

| Từ `IDEA.md §8` | Từ extension upstream |
|---|---|
| `chrome.alarms` mỗi **20 phút** | Bỏ qua nếu cookie **không đổi** |
| `chrome.cookies.getAll` | Debounce theo thời gian |
| Push sang **orchestrator** (`POST /internal/cookie`), không phải parser | |

Hai ý lấy lại đều giảm ghi Redis vô ích. Và **bỏ `console.log` cookie** — extension upstream có.

### Vì sao push sang orchestrator, không phải parser

Parser **không có port ra ngoài** (ràng buộc compose ở Phase 2). Orchestrator có `API_KEY` guard, có logging redact, có `douyin:cookie:meta` để badge đọc. Parser chỉ **đọc** Redis.

---

## 7.2 SSE + health

### `apps/api/events` — NestJS `@Sse()`

Trả `Observable<MessageEvent>`. Phía client dùng **`@microsoft/fetch-event-source`** (không `EventSource` — nó không gửi được custom header, và query param rơi vào access log).

### `HealthBar` — bốn badge

| Badge | Xanh khi | Đỏ nghĩa là |
|---|---|---|
| **Cookie** | `douyin:cookie:meta.last_ok_at < 20 phút` | Extension không chạy, hoặc cookie bị ban |
| **OmniVoice** | `omnivoice:meta.last_ok_at < 5 phút` | Colab chết / URL đổi mà chưa đăng ký |
| **Token page** | mọi `fanpages.token_status = ok` | Page nào đó mất quyền — badge đỏ **per page** |
| **Dung lượng đĩa** | dưới ngưỡng | Sắp đầy |

Badge cookie đọc `sha256` + `last_ok_at` từ meta — **không bao giờ đọc giá trị cookie**.

### Alert "asset đứng yên >6h"

Đây là alert quan trọng nhất của hệ thống, vì nó bắt failure mode **không sinh ra lỗi nào**: stage thiếu lane, lease treo, lane crash im lặng.

`Record<Stage, Lane>` đã làm "thiếu lane" thành lỗi biên dịch, nhưng nó không bắt được lane có mà không chạy.

### Ghi `engine` vào `stage_events`

OmniVoice có **14 TTS engine đổi được ở Settings**. Khi ai đó đổi engine, đây là cách **duy nhất** biết vì sao batch này nghe khác batch kia — API không đổi, chất lượng đổi.

---

## 7.3 `jobs/reconcile.job.ts`

`GET /{video-id}` cho publication ở `scheduled` đã qua `scheduled_at` → cập nhật `published` + `published_at`.

Đây là điều kiện tiên quyết của §7.4: **không được xoá mp4 gốc trước khi biết chắc bài đã lên thật.**

Reconcile cũng là chỗ phát hiện Meta âm thầm không đăng bài (page bị hạn chế, video bị gỡ) — thứ mà Rev 2 không làm được vì `video_reels` không cho read.

---

## 7.4 Job dọn ổ đĩa

```
Xoá /data/source/{sec_user_id}/{aweme_id}.mp4
  KHI mọi rendition của video đó đã publications.status = published
  VÀ reconcile đã xác nhận (không phải chỉ scheduled)
```

Cộng `find -mtime` cho file `.tmp` mồ côi và `/data/dub/*.wav` trung gian.

**Open question #4 phải trả lời trước khi bật cái này:** tự động hay chạy tay? Tự động thì phải chắc `published` đã reconcile từ FB — xoá mp4 gốc của một bài chưa lên thật là mất dữ liệu không lấy lại được **trong horizon rate limit của Douyin**.

Đề xuất: bật ở chế độ **báo cáo trước** (`make disk-report` liệt kê sẽ xoá gì) chạy tay vài tuần, rồi mới `@Cron`.

---

## 7.5 Preflight dung lượng đĩa

Trước job lớn: kiểm còn đủ chỗ không. **Đầy đĩa là lỗi cứng** — đây là cái giá đã chấp nhận khi chọn ổ đĩa thay MinIO ở D5.

Sizing: ~25 GB cho giai đoạn test (1.000 × 15MB gốc + 10MB output). +5–8 GB/ngày khi mở rộng.

---

## 7.6 Backup

`rsync` cho `/data` + `pg_dump` cho Postgres.

**Không cần replication cho `/data`** — dữ liệu **tái tạo lại được** (mp4 gốc tải lại từ Douyin, output dub lại bằng GPU). Cái cần biết không phải "làm sao không mất" mà **"mất thì bao lâu dựng lại được"** — đo một lần, ghi vào runbook.

Postgres thì **khác hẳn**: `renditions.stage`, `publications.fb_video_id`, `fanpages.access_token_enc` **không tái tạo được**. `pg_dump` hàng ngày, giữ 30 bản.

> Phân biệt này quan trọng: một backup strategy đối xử với `/data` và Postgres như nhau sẽ vừa tốn quá nhiều cho `/data`, vừa quá ít cho Postgres.

---

## 7.7 Runbook — `docs/runbook.md`

Sáu tình huống, mỗi cái một đoạn ngắn:

1. **Cookie bị ban** → job `blocked`, badge đỏ. Lấy cookie mới, extension push, job resume từ `max_cursor`.
2. **Colab chết** → OmniVoice badge đỏ. Dựng lại, notebook tự `POST /internal/omnivoice`. Rendition đang chạy: poll 404 ⇒ tự enqueue lại. **Không phải làm gì.**
3. **Page token invalid** → badge đỏ per page. Dán lại 1 user token → `/me/accounts` refresh toàn bộ.
4. **Talent rời agency** → `is_active=false` chặn rendition mới. Bài **đã hẹn** thì `DELETE /{video-id}` — có code, không cần tay.
5. **Đầy đĩa** → `make disk-report`, xoá mp4 gốc đã publish hết.
6. **Douyin đổi `a_bogus`** → tỉ lệ job `blocked` tăng vọt. Pull upstream, chạy checklist 5 mục của `PATCH_NOTES.md`, kiểm `git diff` vẫn ≤50 dòng.

---

## 7.8 Bảo trì định kỳ

| Việc | Nhịp | Vì sao |
|---|---|---|
| Pull upstream fork + chạy `PATCH_NOTES.md` | Hàng tuần | Douyin đổi `a_bogus`. **Điểm ma sát định kỳ duy nhất của hệ thống** |
| Kiểm vài bài còn ở tab Reels không | Hàng tháng | Auto-convert của Meta là **quyết định sản phẩm, không phải hợp đồng API** — nó đổi được mà không có deprecation notice |
| Review version Graph API | 6 tháng | Pages API v18 đã tắt 26/01/2026 |
| Kiểm `expires_at` của user token trong `fb_credentials` | Trước mốc 60 ngày | Hết hạn không làm rớt đăng bài, nhưng mất khả năng sync page mới |

---

## Definition of done

- [ ] Extension push cookie mỗi 20 phút, **không phụ thuộc có ai mở Douyin**
- [ ] Extension không log cookie, bỏ qua khi cookie không đổi
- [ ] Bốn badge health hoạt động, badge token đỏ **per page**
- [ ] Alert "asset đứng yên >6h" đã bắn thử một lần (dựng tình huống)
- [ ] Reconcile cập nhật `published` + `published_at` từ FB
- [ ] `make disk-report` liệt kê đúng file xoá được
- [ ] `pg_dump` + `rsync` chạy tự động, **đã thử restore một lần**
- [ ] `docs/runbook.md` có 6 tình huống
