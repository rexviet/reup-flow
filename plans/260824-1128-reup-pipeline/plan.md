---
name: reup-pipeline
title: "Reup Pipeline — Douyin → Dub tiếng Việt → Facebook"
status: in_progress
created: 2026-08-24
source: docs/reports/solution-design-2026-08-24-reup-pipeline.md
sourceRev: 5
blockedBy: []
blocks: []
phases: 8
---

# Implementation Plan — Reup Pipeline

**Nguồn:** `docs/reports/solution-design-2026-08-24-reup-pipeline.md` (Rev 5) · `IDEA.md`
**Stack đã chốt:** TypeScript / NestJS monorepo · Postgres 16 + Drizzle · Redis · ổ đĩa (không MinIO) · parser Python fork (submodule) · OmniVoice Studio (dubbing) · Facebook Graph `POST /{page-id}/videos`

---

## 0. Nguyên tắc bất khả xâm phạm

Ba điều này quyết định plan có đúng hay không, và **mọi phase phải tôn trọng**:

1. **Ba module chỉ nối nhau qua state trong DB.** Không HTTP call, không message, không import xuyên module. Enforce hai tầng: ESLint `no-restricted-imports` (bắt lúc build) + `module-isolation` test (bắt lúc CI).
2. **`renditions.stage` / `videos.stage` là chân lý duy nhất.** Job id của OmniVoice, video id của FB đều là dữ liệu phụ, mất được.
3. **Secret không bao giờ vào log.** Ba secret ba chỗ: cookie Douyin (Redis) · page token (Postgres, AES-256-GCM) · `FB_APP_SECRET` (chỉ env). Filter redact phủ cả ba, có test CI.

---

## 1. Lệch so với roadmap §13 của solution design — đọc trước khi bắt đầu

Solution design xếp *"Lane framework + Module 2"* vào M3, nhưng **M1 (enumerate + download) đã là hai lane**. Nếu làm đúng thứ tự M0→M1→M2→M3 thì Module 1 sẽ được viết bằng vòng lặp tay rồi phải viết lại khi lane framework ra đời.

**Plan này tách lane framework thành Phase 2 (trước Module 1)** và giữ nguyên mọi thứ khác. Đây là lệch duy nhất so với §13, và nó làm plan có 8 phase thay vì 6 milestone.

Map plan ⇄ milestone của solution design:

| Phase | Milestone §13 | Ghi chú |
|---|---|---|
| Phase 0 — Spikes | §13 bước 2 | **Gate: có thể huỷ dự án** |
| Phase 1 — Repo + parser patch | M0 | |
| Phase 2 — Platform (db + lanes + ports) | *(tách ra từ M3)* | Lệch có chủ ý, xem trên |
| Phase 3 — Module 1 Ingest | M1 | |
| Phase 4 — Một rendition làm tay | M2 | **Gate: go/no-go chất lượng dub** |
| Phase 5 — Module 2 Dubbing đầy đủ | M3 | |
| Phase 6 — Module 3 Publish | M4 | |
| Phase 7 — Automation & Ops | M5 | |

---

## 2. Hai gate cứng

```
Phase 0 ──gate A──> Phase 1..3 ──> Phase 4 ──gate B──> Phase 5..7
```

**Gate A — sau Phase 0.** Ba câu phải có câu trả lời bằng bằng chứng, không phải phán đoán:
- Voice clone giọng talent đọc tiếng Việt **nghe được** hay không? → nếu không, dự án dừng (một ngôn ngữ đích, không đường lùi).
- `POST /{page-id}/videos` + `published=false` + `scheduled_publish_time` có **lên tab Reels** không? → nếu không, đổi sang D3-A3 (dùng cả hai endpoint) và Phase 6 phình gấp đôi.
- Page token dài hạn có thật sự trả `expires_at: 0` không? → phép kiểm nhị phân của D6 dựa vào đúng con số này.

**Gate B — sau Phase 4.** Một rendition thật, làm bằng tay, nghe bằng tai người. **Đừng nhập Phase 4 vào Phase 5.** Nếu chất lượng dub tệ, cần biết ở ngày thứ 3 chứ không phải tuần thứ 6.

---

## 3. Phase index

| # | File | Nội dung | Effort | Blocked by | Status |
|---|---|---|---|---|---|
| 0 | `phase-00-spikes.md` | 5 spike xác nhận thiết kế | ~1–2 ngày | — | **not_started** |
| 1 | `phase-01-repo-parser.md` | git init · submodule · patch cookie · 4 pytest | ~0.5 ngày | 0 | ✅ **completed** |
| 2 | `phase-02-platform.md` | monorepo · drizzle schema · lane framework · ports · shared | ~3–4 ngày | 1 | not_started |
| 3 | `phase-03-ingest.md` | Module 1: enumerate + download + caption gốc + stat | ~3 ngày | 2 | not_started |
| 4 | `phase-04-dub-poc.md` | 1 rendition làm tay → **nghe** | ~0.5 ngày | 3 | not_started |
| 5 | `phase-05-dubbing.md` | Module 2: curation · dub · caption · render · review UI | ~5–6 ngày | 4 | not_started |
| 6 | `phase-06-publish.md` | Module 3: onboarding token · distributor · slot · publish | ~4 ngày | 5 | not_started |
| 7 | `phase-07-automation.md` | extension · SSE · health · reconcile · dọn đĩa | ~3 ngày | 6 | not_started |

Effort là **thời gian code**, không tính chờ review người và không tính GPU chạy.

**Phase 1 đã chạy trước Phase 0**, dù `blockedBy: [0]`. Lý do: ba câu của Gate A — chất lượng voice clone, endpoint Reels, `expires_at` của page token — không chạm gì tới output của Phase 1 (repo, cơ chế đổi cookie, secret scan). Không có câu trả lời nào của Gate A làm Phase 1 phải viết lại. **Gate A vẫn còn nguyên và vẫn chặn từ Phase 3 trở đi.**

---

## 4. Quyết định đã chốt — không mở lại trong lúc implement

Nếu một quyết định ở bảng này bị nghi ngờ giữa lúc code, **dừng lại và sửa solution design trước**, đừng lặng lẽ đi đường khác.

| ID | Quyết định | Hệ quả cụ thể lên code |
|---|---|---|
| D1 | Asset state machine trên Postgres, không broker | Không có `bullmq`, không có `pg-boss`. Lane claim bằng `FOR UPDATE SKIP LOCKED` |
| D2 | OmniVoice `POST /batch/enqueue` (1 lane) | Module 2 còn 3 lane. Leo lên đường granular **chỉ khi** `reject_reason` chứng minh cần |
| D3 | `POST /{page-id}/videos`, không phải Reels API | Horizon 6 tháng ⇒ **không có top-up lane**. Có `DELETE`, có `GET` reconcile |
| D4 | Gán slot ngay lúc approve | Không có lane định kỳ nào cho publish. `pinned` cho can thiệp tay |
| D5 | Ổ đĩa + interface `Storage` | Không có MinIO trong compose. `api` và `worker` **bind cùng `/data`** |
| D6 | Dán **1** user token ngắn hạn, tool tự exchange | Không OAuth redirect, không App Review. `FB_APP_SECRET` vào env |
| D7 | Drizzle ORM | `Record<Stage, Lane>` ⇒ thiếu lane là **lỗi biên dịch** |

---

## 5. Risk register — chỉ những cái đổi được hành vi lúc implement

| Risk | Phase chịu trách nhiệm | Cách xử lý trong code |
|---|---|---|
| **Cookie thật trong `crawlers/douyin/web/config.yaml`** — *đã kiểm 2026-08-24: là cookie của UPSTREAM (`Evil0ctal`, commit `3b491db`, 2025-03-16), public từ 2024, không phải cookie của ta* | Phase 1 | ✅ **XONG (Phase 1).** Scrub thành `Cookie: ""`, fallback qua env `DOUYIN_COOKIE_FALLBACK`. Thêm rule `douyin-session-cookie` riêng + bước canary trong CI — **ruleset mặc định của gitleaks không nhìn thấy cookie Douyin**: nó chỉ bắt được cookie upstream vì 3 trong ~40 field tình cờ là base64 entropy cao; một cookie hex thường lọt sạch. Canary tự trồng cookie mỗi lần chạy CI và fail nếu scanner không còn phát hiện được. |
| OmniVoice job state in-memory, mất khi restart | Phase 5 | Poll trả **404 ⇒ enqueue lại**, không phải `failed` |
| OmniVoice down đốt sạch `attempts` | Phase 2 (lane base) + Phase 5 | Health gate **trước khi claim**. Fail ⇒ nhả lease, **không tăng `attempts`**. 409 (thiếu ASR model) cũng vậy |
| Asset kẹt vì stage thiếu lane | Phase 2 | `Record<Stage, Lane>` + alert "asset đứng yên >6h" |
| `advance()` không atomic với ghi file | Phase 2 | Thứ tự cứng: ghi `.tmp` → `fs.rename` → **rồi mới** commit transaction |
| Token 1 giờ lọt vào hệ thống | Phase 6 | `GET /debug_token`, **chỉ nhận `expires_at == 0`**. Phép kiểm nhị phân, không phải ngưỡng |
| `scheduled_publish_time` vượt 6 tháng | Phase 6 | `HORIZON_DAYS=150` cắt cứng, có test |
| Reviewer thấy video không có caption | Phase 5 | Vào `pending_review` chỉ khi **cả** dub **và** caption xong |
| Đầy ổ đĩa | Phase 7 | Preflight check trước job lớn + alert ngưỡng + job xoá mp4 gốc đã publish hết |
| Douyin đổi `a_bogus` | Phase 1 + vận hành | Pull upstream hàng tuần theo `PATCH_NOTES.md`; alert tỉ lệ job `blocked` |

---

## 6. Open questions — không chặn code, cần trả lời trước Phase 6/7

1. **Ai là reviewer, mỗi rendition duyệt mất bao lâu thật?** Đo từ Phase 5 ngày đầu. Chặn kế hoạch mở rộng, không chặn code.
2. **`default_hashtags` cho từng page.** Để trống thì Phase 6 vẫn chạy, caption chỉ có prose.
3. **`HORIZON_DAYS`** — plan này dùng **150**. Đổi được bằng env.
4. **Job dọn ổ đĩa tự động hay chạy tay?** Quyết ở Phase 7. Tự động thì phải chắc `published` đã reconcile từ FB.

---

## 6.5 Phase 1 để lại gì cho Phase 2/3

1. **`requirements.txt` của fork giờ cần `redis`** (đã pin `redis==8.1.0`), và `requirements-dev.txt` cho test. Compose ở Phase 2 phải có Redis **trước khi** parser chạy thật — không có Redis thì cookie rơi về `DOUYIN_COOKIE_FALLBACK`.
2. **Đường async đã xong, không cần Phase 3 lo.** `get_douyin_headers()` giờ `await resolve_cookie_async(...)`, dùng `redis.asyncio`, và client được cache **theo từng event loop**. Loop bị đóng/tạo lại thì client tự dựng lại — cookie **không** đứng. Đây là bản sửa, không phải hạn chế: bản cũ cache ở module level nên sau khi loop đổi là cookie đóng băng vĩnh viễn cả process mà không có lỗi nào.
3. **`_consecutive_failures` dùng được cho health bar ở Phase 7.** Tăng khi Redis lỗi, về 0 khi thành công. Warning bị nén tối đa 60s/lần nên **đừng đếm dòng log** — đọc counter.
4. **`socket_timeout=1` / `socket_connect_timeout=1` là bắt buộc**, không phải tuning. Bỏ đi thì Redis treo sẽ treo mọi request Douyin. Có test giữ.
5. **`POST /api/hybrid/update_cookie` của upstream giờ chết** — `update_cookie()` đã xoá. Cố ý: nó ghi cookie do caller đưa vào file tracked và không có auth. Ai báo endpoint đó 500 thì đúng như thiết kế.

---

## 7. Definition of done cho cả plan

- [ ] 1.000 video đi hết pipeline: Douyin → dub tiếng Việt → duyệt → hẹn giờ trên 10 fanpage
- [ ] `module-isolation` test xanh — nạp tay một `rendition` ở `approved` thì Module 3 vẫn chạy
- [ ] `secrets-never-logged` xanh cho cả ba secret
- [ ] Đổi key Redis là parser dùng cookie mới, **không restart**
- [ ] Fork lệch upstream trong giới hạn: **37/50 dòng ở file upstream cũng sở hữu** (xem `scripts/check-patch-size.sh` — gate 3 tầng, không phải một con số ≤50 cho toàn bộ diff; lý do ghi ở `phase-01-repo-parser.md`)
- [ ] Restart worker giữa lúc chạy: không mất asset, không xử lý trùng
- [ ] Health bar xanh: cookie · OmniVoice · token page · dung lượng đĩa
