---
type: brainstorm-summary
title: "Dubbing Service — brainstorm summary"
date: 2026-08-27
status: superseded
source: conversation
next: solution-design
---

# Dubbing Service — Brainstorm Summary

**Ngày:** 2026-08-27. Nguồn: yêu cầu trực tiếp + code thật trong repo.

---

## 1. Problem statement

Downloader service đã hoàn thành: enumerate → download all videos từ Douyin profile. Cần thêm **dubbing service** — service riêng biệt (separate NestJS app, separate Docker container) tự động dub video đã tải về bằng **OmniVoice Studio** (chạy trên Google Colab T4), Trung → Việt.

**Trigger:** mỗi video download xong (`videos.status = 'done'`) → tạo 1 dubbing job trong cùng transaction. Dubbing service poll Postgres, claim job, gửi video lên OmniVoice, poll/nhận kết quả, lưu output.

### Requirements đã xác nhận

| Yếu tố | Quyết định |
|---|---|
| Trigger | Per-video, ngay khi `videos.status = 'done'`. Downloader insert `dubbing_jobs` trong cùng transaction |
| Target | OmniVoice Studio trên Google Colab T4, giao tiếp qua HTTP REST API |
| Pipeline | OmniVoice xử lý full A→Z (tách audio, dịch, TTS, mix). Service chỉ gửi video + poll/nhận kết quả |
| Concurrency | Configurable. Mỗi container chạy N lane (giống download lane pattern). Scale = thêm container |
| Error handling | Retry với exponential backoff (giống downloader). Colab hay disconnect |
| Output | Chưa rõ format trả về (cần discover khi tích hợp OmniVoice API) |
| Language | Trung → Việt |
| UI | Thanh progress per-video trên web UI |
| Infra | Cùng Postgres + Redis. Separate Docker container |

### Out of scope (v1)

Multi-language (chỉ zh → vi) · Manual video selection (tất cả auto) · Upload dubbed video lên platform (YouTube, TikTok) · Subtitle extraction riêng.

---

## 2. Trạng thái repo (đọc từ code)

| Hạng mục | Trạng thái |
|---|---|
| Downloader service (`services/downloader/`) — NestJS + Drizzle + Postgres + Redis | ✅ hoàn thành |
| `LaneRunner` base class + `LaneSupervisor` + reaper pattern | ✅ proven, battle-tested |
| `download.lane.ts` — `SKIP LOCKED` claim, lease, heartbeat, ownership re-check | ✅ hoàn thành |
| `videos.status` lifecycle: pending → downloading → done/failed/skipped | ✅ hoàn thành |
| Web UI (`services/web/`) — Vite + React | ✅ có |
| Chrome extension (`services/extension/`) | ✅ có |
| Docker compose — nest + parser + postgres + redis + web | ✅ có |
| OmniVoice Studio integration | ❌ chưa có gì |
| Dubbing service | ❌ chưa có gì |
| Shared package `@reup-flow/shared` | ❌ chưa có |

**Kết luận:** downloader stack hoàn chỉnh, dubbing là greenfield service dùng cùng infra.

---

## 3. Evaluated approaches

### Approach 1 — Clone Architecture (service riêng, clone LaneRunner pattern) ★ RECOMMENDED

**Ý tưởng:** tạo `services/dubbing/` — NestJS app riêng, separate container. Clone `LaneRunner` + `LaneSupervisor` + reaper pattern từ downloader. Postgres-only coupling.

**Kiến trúc:**

```
┌──────────────────────────────────────────────────┐
│  DOWNLOADER SERVICE (existing)                    │
│  download.lane.ts                                 │
│    video status=done ──→ INSERT dubbing_jobs       │
│                          (same transaction)        │
└──────────────────┬───────────────────────────────┘
                   │ Postgres (only channel)
┌──────────────────▼───────────────────────────────┐
│  DUBBING SERVICE (new NestJS app)                 │
│                                                   │
│  ┌──────────────┐   ┌──────────────┐              │
│  │ Dubbing Lane │   │ Reaper Lane  │              │
│  │ (N instances)│   │ (orphan      │              │
│  │              │   │  recovery)   │              │
│  └──────┬───────┘   └──────────────┘              │
│         │                                         │
│  ┌──────▼───────┐                                 │
│  │ OmniVoice    │                                 │
│  │ Client       │ ─── HTTP ───→ OmniVoice Studio  │
│  └──────────────┘               (Colab T4)        │
└───────────────────────────────────────────────────┘
```

**Schema mới (cùng DB):**

```sql
CREATE TABLE dubbing_jobs (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  aweme_id         TEXT NOT NULL REFERENCES videos(aweme_id),
  status           TEXT NOT NULL DEFAULT 'pending',  -- pending·processing·done·failed
  remote_job_id    TEXT,           -- OmniVoice's job/task ID for polling
  output_path      TEXT,           -- dubbing output file path
  source_lang      TEXT NOT NULL DEFAULT 'zh',
  target_lang      TEXT NOT NULL DEFAULT 'vi',
  attempts         INTEGER NOT NULL DEFAULT 0,
  last_error       TEXT,
  lease_expires_at TIMESTAMPTZ,    -- F3-style lease for orphan recovery
  progress         INTEGER,        -- 0-100 from OmniVoice, nullable
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX dubbing_jobs_pending_idx
  ON dubbing_jobs(status, created_at) WHERE status = 'pending';
CREATE INDEX dubbing_jobs_processing_lease_idx
  ON dubbing_jobs(status, lease_expires_at) WHERE status = 'processing';
CREATE INDEX dubbing_jobs_aweme_id_idx
  ON dubbing_jobs(aweme_id);
```

**Dubbing lane flow:**

1. `claimOne()` — `FOR UPDATE SKIP LOCKED` trên `dubbing_jobs WHERE status = 'pending'`
2. Lấy video path từ `videos.storage_path` qua `aweme_id`
3. Gửi video lên OmniVoice Studio (upload hoặc URL tuỳ API)
4. Poll OmniVoice status → update `dubbing_jobs.progress`
5. Download result → lưu vào storage
6. `markDone()` hoặc `markFailed()` + lease release

**Ưu điểm:**

- Giữ nguyên invariant plan §0 principle 1: Postgres là kênh duy nhất giữa 2 service
- Proven pattern: `LaneRunner` + reaper đã battle-tested. Copy + adapt, không invent mới
- Scale tự nhiên: `DUBBING_CONCURRENCY=N` → N lane giống `DOWNLOAD_CONCURRENCY`
- Resilient: Colab T4 hay disconnect → lease + reaper + retry xử lý sẵn
- Separation of concerns: downloader chỉ `INSERT`, dubbing chỉ `poll` — thêm/bỏ không ảnh hưởng nhau

**Nhược điểm:**

- Boilerplate: setup NestJS app mới (package.json, Dockerfile, app.module, config...)
- Code duplication: `LaneRunner`, `sleep.ts`, `redactor`, DB module cần copy hoặc extract shared package
- Downloader phải biết schema `dubbing_jobs` để INSERT (coupling tại write point)

---

### Approach 2 — Module trong downloader (thêm DubbingModule vào downloader service)

**Ý tưởng:** thêm `DubbingModule` + dubbing lane trực tiếp vào downloader, chạy chung process.

```
┌──────────────────────────────────────────────────┐
│  DOWNLOADER SERVICE (existing + new module)       │
│                                                   │
│  ┌───────────────┐  ┌───────────────┐             │
│  │ Download Lane │  │ Dubbing Lane  │ ← NEW       │
│  │ (6 instances) │  │ (N instances) │             │
│  └───────────────┘  └───────────────┘             │
│         │                    │                     │
│         └─── same DB, same process ───┘           │
│                    │                               │
│              OmniVoice Client ──→ Colab T4        │
└──────────────────────────────────────────────────┘
```

**Ưu điểm:**

- Nhanh nhất: zero bootstrap boilerplate. Thêm module + lane + table
- Reuse tối đa: `LaneRunner`, `LaneSupervisor`, DB, Redis, config, redactor — tất cả có sẵn
- Deployment đơn giản: 1 container, 1 Dockerfile

**Nhược điểm:**

- **Vi phạm principle §0**: downloader "biết" dubbing → scope creep, trở thành God service
- **Coupling cứng**: dubbing lane fail liên tục (Colab disconnect) gây noise chung với download lane
- **Scale lệch**: download cần 6 lane, dubbing 1-2. Chạy chung → scale cả cụm hoặc thêm flag phức tạp (`DUBBING_LANES_ENABLED`)
- **Colab timing mismatch**: mỗi dubbing job mất 5-15 phút vs download job mất giây. `staleAfterMs` phải tune riêng, khác hoàn toàn
- Nếu sau này cần scale dubbing riêng (GPU machine khác) → phải tách ra anyway

---

### Approach 3 — Event-driven (Redis pub/sub + worker)

**Ý tưởng:** downloader publish `video.downloaded` event qua Redis pub/sub. Dubbing worker subscribe + xử lý.

```
Downloader ──pub──→ Redis channel "video.downloaded"
                         │
              ┌──────────▼──────────┐
              │  Dubbing Worker     │
              │  (subscriber)       │
              │  ── HTTP ──→ Colab  │
              └─────────────────────┘
```

**Ưu điểm:**

- Loosely coupled: downloader không biết dubbing exists
- Extensible: future services subscribe cùng channel

**Nhược điểm:**

- **Redis pub/sub = at-most-once**: worker chưa subscribe lúc event publish → MẤT. Không durable
- Phải đổi sang Redis Streams hoặc BullMQ → thêm complexity
- Mất mô hình `SKIP LOCKED` đã proven → implement distributed locking kiểu khác
- Downloader hiện không publish gì → thêm event publishing layer
- **Overengineered**: chỉ có 1 consumer (dubbing), chưa cần fan-out

---

## 4. Comparison matrix

| Tiêu chí | A1 — Clone ★ | A2 — Module | A3 — Event |
|---|---|---|---|
| Thời gian ship | Medium (2-3 ngày) | Fast (1-2 ngày) | Slow (3-4 ngày) |
| Separation of concerns | ⭐⭐⭐ | ⭐ | ⭐⭐⭐ |
| Code reuse | ⭐⭐ (copy/share) | ⭐⭐⭐ | ⭐ |
| Scale independence | ⭐⭐⭐ | ⭐ | ⭐⭐⭐ |
| Resilience (Colab flaky) | ⭐⭐⭐ | ⭐⭐ | ⭐⭐ |
| Complexity | Low-Medium | Low | High |
| Proven pattern | ⭐⭐⭐ | ⭐⭐⭐ | ⭐ |

---

## 5. Final recommendation: Approach 1 — Clone Architecture

### Rationale

1. **Colab T4 là bottleneck lớn nhất.** Mỗi dubbing job mất phút, không phải giây. Chạy chung process với download (A2) tạo coupling vô ích — dubbing lane bị block 10 phút/video trong khi download lane xử lý 6 video/phút. Tách ra cho clean.

2. **Pattern đã proven.** `LaneRunner` + lease + reaper + `SKIP LOCKED` battle-tested trong downloader. Copy pattern, thay `fetchVideo()` bằng `callOmniVoice()`. Không invent mới.

3. **Postgres-only coupling giữ principle §0.** Downloader `INSERT dubbing_jobs` trong transaction mark video `done`. Dubbing service `SELECT ... FOR UPDATE SKIP LOCKED WHERE status = 'pending'`. Cùng DB, zero RPC giữa 2 service.

4. **Colab hay chết.** Separate container → restart dubbing service không ảnh hưởng download. Retry + lease recovery xử lý disconnect tự nhiên.

5. **Boilerplate là chi phí 1 lần.** Setup NestJS app mất vài giờ, sau đó mỗi thay đổi dubbing-specific không chạm downloader.

### Risks

| Rủi ro | Mitigation |
|---|---|
| OmniVoice API chưa rõ (chưa biết nhận gì, trả gì) | Viết `OmniVoiceClient` interface trước, stub. Khi có API doc thì swap |
| Colab Notebook runtime limit (12h free, 24h Pro) | Lease timeout > thời gian dub 1 video. Reaper recovery khi Colab bị ngắt |
| Code duplication (LaneRunner, sleep, redactor...) | Phase 1: copy. Nếu service thứ 3 → extract `@reup-flow/shared` |
| Video file transfer tới Colab | Cần discover: upload HTTP? Google Drive? ngrok? **Unknown lớn nhất** |
| Schema coupling (downloader viết `dubbing_jobs`) | Chấp nhận. Insert-only, 1 dòng SQL |

### Implementation sketch

```
services/dubbing/
├── Dockerfile
├── package.json
├── tsconfig.json
├── migrations/
│   └── 260827-dubbing-schema.sql
└── src/
    ├── main.ts
    ├── bootstrap.ts
    ├── app.module.ts
    ├── config/
    │   ├── config.module.ts
    │   └── env.ts                  # OMNIVOICE_BASE_URL, DUBBING_CONCURRENCY...
    ├── db/
    │   ├── db.module.ts            # Same Postgres connection
    │   ├── schema.ts               # dubbing_jobs table
    │   ├── dubbing-jobs.repository.ts
    │   └── executor.ts
    ├── omnivoice/
    │   ├── omnivoice.client.ts     # HTTP client: upload + poll
    │   ├── omnivoice.interface.ts  # Contract (stub-able)
    │   └── omnivoice.module.ts
    ├── lanes/
    │   ├── dubbing.lane.ts         # Claim → upload → poll → download result → done
    │   ├── reaper.lane.ts          # Orphan recovery
    │   ├── lane.runner.ts          # Copy from downloader (for now)
    │   ├── lane.supervisor.ts
    │   └── lanes.module.ts
    ├── storage/
    │   ├── storage.interface.ts
    │   └── local-disk.storage.ts
    └── health/
        └── health.controller.ts
```

---

## 6. Open questions

1. **OmniVoice API contract** — Có tài liệu API hoặc code example không? Cần biết: nhận video bằng cách nào (upload? URL? Drive ID?), polling hay webhook, response format, authentication.

2. **File transfer mechanism** — OmniVoice trên Colab T4 có thể access file bằng cách nào? Upload HTTP (Colab URL hay thay đổi)? Shared Google Drive? ngrok tunnel? Đây quyết định kiến trúc storage layer.

3. **Dubbed output storage** — Video đã dub lưu ở đâu? Cùng thư mục gốc (`douyin/{sec_user_id}/{aweme_id}_vi.mp4`)? Thư mục riêng?

4. **Schema coupling strategy** — OK với downloader INSERT `dubbing_jobs` trực tiếp? Hay dubbing service tự poll `videos WHERE status = 'done' AND NOT EXISTS (SELECT 1 FROM dubbing_jobs ...)`?

---

## 7. Next steps

Sau khi align open questions → `/solution-design` → `/plan` → `/cook`.
