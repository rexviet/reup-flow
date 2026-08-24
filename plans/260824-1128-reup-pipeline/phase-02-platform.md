---
phase: 2
title: Platform — monorepo, DB schema, lane framework, ports
status: not_started
effort: 3-4 ngày
blockedBy: [1]
---

# Phase 2 — Platform

Phase này **không có tính năng nào cho người dùng**. Nó xây cái mà cả ba module đứng lên. Tách ra vì lane framework là hạ tầng của Module 1, không phải của Module 2 như §13 xếp.

---

## 2.1 Monorepo — NestJS monorepo mode + pnpm workspace

```
reup-flow/
  pnpm-workspace.yaml · package.json · tsconfig.base.json
  docker-compose.yml · .env.example · Makefile
  src/Douyin_TikTok_Download_API/     ← submodule (Phase 1)
  apps/
    api/         NestJS — HTTP + SSE. KHÔNG chạy lane
    worker/      NestJS — CHỈ chạy lane. Không mở port
    web/         React + Vite
    extension/   MV3 (Phase 7)
  libs/
    db/           drizzle schema + migrations + client
    lanes/        Lane base · registry · runner · reaper · graceful · gpu semaphore
    ingest/       Module 1 domain
    dubbing/      Module 2 domain
    publish/      Module 3 domain
    ports/        VideoDistributor · Storage · TokenProvider
    shared/       config · logging+redaction · crypto · errors
```

`api` và `worker` là **hai NestJS app dùng chung `libs/`**, không phải hai repo. Tách vì worker không nên mở port và api không nên claim việc.

### Enforce nguyên tắc kiến trúc số 1 bằng ESLint

```jsonc
// .eslintrc — trong libs/publish
"no-restricted-imports": ["error", { "patterns": [
  "**/libs/dubbing/**", "**/libs/ingest/**"
]}]
```

Rẻ hơn và bắt sớm hơn `module-isolation` test. Cả hai đều giữ, không phải chọn — ESLint bắt lúc gõ, test bắt trường hợp lách qua bằng dynamic import.

---

## 2.2 `.env.example` + zod validate

```
API_KEY · DATABASE_URL · REDIS_URL · PARSER_BASE_URL · DATA_DIR=/data
ENCRYPTION_KEY            # 32 byte base64
FB_APP_ID · FB_APP_SECRET
LLM_API_KEY
LANE_CONCURRENCY          # JSON: {"enumerate":1,"download":8,"dub":1,...}
GPU_SEMAPHORE_SIZE=1
URL_TTL_SECONDS=7200 · LEASE_SECONDS · HORIZON_DAYS=150
DISTRIBUTOR=round_robin
```
Phía fork: `REDIS_URL` · `DOUYIN_COOKIE_FALLBACK`.

`shared/config.ts` dùng `@nestjs/config` + **zod validate, fail fast lúc bootstrap**. Thiếu `ENCRYPTION_KEY` phải là crash lúc khởi động, không phải lỗi lúc dán token đầu tiên.

**`GPU_SEMAPHORE_SIZE=1` không phải một lựa chọn tuning.** `_worker()` của OmniVoice xử lý một job một lúc — đây là khớp với thực tế bên kia. Nâng lên chỉ tạo hàng đợi ở tầng sai.

---

## 2.3 `docker-compose.yml`

Service: `api` · `worker` · `parser` · `postgres` · `redis` · `web`. **Không có `minio`.**

Hai ràng buộc phải ghi thành comment trong file, vì cả hai sẽ **vỡ âm thầm**:

```yaml
  parser:
    # KHÔNG có ports: — parser không được expose ra ngoài internal network
  api:
    volumes: [ /data:/data ]   # api và worker PHẢI bind CÙNG /data (D5).
                               # api cần đọc file để upload lên FB và serve preview review UI.
                               # Tách deploy hai container này ra hai máy sẽ vỡ mà không có lỗi rõ ràng.
  worker:
    volumes: [ /data:/data ]
```

Redis: bật **AOF**, `maxmemory-policy noeviction`. Không dùng chung instance này làm cache — cookie bị evict là job `blocked`.

---

## 2.4 `libs/shared/`

| File | Nội dung |
|---|---|
| `config.ts` | zod schema cho env, fail fast |
| `logging.ts` | **redact**: `sessionid`, `passport_`, `ttwid`, `msToken`, `access_token`, `EAA*`, `FB_APP_SECRET`, `Cookie` |
| `crypto.ts` | AES-256-GCM (`node:crypto`), lưu `iv ‖ authTag ‖ ciphertext` |
| `errors.ts` | phân biệt `TransientError` (retry, tăng `attempts`) · `SetupError` (nhả lease, **không** tăng `attempts`) · `PermanentError` (`failed` ngay) |

**`errors.ts` là chỗ quyết định hành vi retry của cả hệ thống.** Ba loại lỗi, ba hành vi khác nhau — nếu gộp lại thì OmniVoice down sẽ đốt sạch `attempts` của hàng trăm rendition.

> Mọi chỗ trong solution design ghi "Fernet" phải đọc là **AES-256-GCM**. Không có implementation Fernet đáng tin cho Node, và GCM đã có authenticated encryption sẵn.

---

## 2.5 `libs/db/` — Drizzle schema

`db/schema.ts` là **nguồn của cả bảng và type `Stage`**. Đây là lợi ích cụ thể của D7-A1: enum khai một chỗ, `Record<Stage, Lane>` suy ra từ đó.

### Bảng theo module (chi tiết cột ở §6 của solution design)

**Module 1:** `talents` · `talent_voices` · `videos` · `jobs`
**Module 2:** `talent_glossary` · `languages` · `renditions`
**Module 3:** `fanpages` · `publications` · `fb_credentials`
**Chung:** `stage_events`

### Ba chi tiết dễ làm sai

1. **`videos.description`**, không phải `desc` — `desc` là từ khoá SQL. Map lại ở ORM. Đây là caption gốc của Douyin, **lưu thô nguyên văn**, không xử lý gì ở Module 1.
2. **`renditions.stage`** ∈ `{pending_dub, pending_caption, pending_render, pending_review, approved, rejected, failed}`.
   - **Không có `pending_prepare`** — probe gộp vào render (D3 xoá gate 3–90s nên không cần lane chặn sớm).
   - **Không có `ineligible`** — D3-A2 không gate duration.
3. **`renditions.reject_reason` là enum**, không phải text: `bad_translation | bad_voice | bad_timing | bad_audio_mix | bad_caption | content_unsuitable | other`. Chỉ enum mới cho biết tua về stage nào, và mới đo được engine yếu ở đâu. **Đây là tín hiệu quyết định có leo lên D2-A2 hay không** — text tự do làm mất luôn khả năng đo.

### Index bắt buộc

```sql
-- câu claim của MỌI lane
CREATE INDEX ON renditions (stage, next_attempt_at) WHERE status = 'ready';
CREATE INDEX ON videos     (stage, next_attempt_at) WHERE status = 'ready';
-- slot allocator
CREATE INDEX ON publications (fanpage_id, scheduled_at);
```

### Migration
`db/migrations/` sinh bằng `drizzle-kit generate` → **file SQL thường, review được trong PR**. Greenfield, online-safe. Seed `languages = ('vi','Tiếng Việt',true)`.

> `languages` giữ nguyên dù chỉ một hàng. "Làm một thị trường **trước**" nghĩa là thị trường thứ hai là `INSERT` một hàng, không phải một migration.

---

## 2.6 `libs/lanes/` — lane framework

```ts
export abstract class Lane<TItem> {
  abstract readonly stage: Stage
  abstract readonly concurrency: number
  protected abstract claim(tx: Tx): Promise<TItem | null>
  protected abstract handle(item: TItem): Promise<StageTransition>
}
```

Câu claim — lý do chọn Drizzle:

```ts
const [row] = await tx.select().from(renditions)
  .where(and(
    eq(renditions.stage, this.stage),
    eq(renditions.status, 'ready'),
    or(isNull(renditions.nextAttemptAt), lte(renditions.nextAttemptAt, sql`now()`)),
  ))
  .orderBy(renditions.createdAt).limit(1)
  .for('update', { skipLocked: true })        // first-class, có type
```

| File | Nhiệm vụ | Điều dễ làm sai |
|---|---|---|
| `lane.base.ts` | claim + lease + renew + `advance()` + backoff + `attempts` | **`advance()` phải atomic với ghi artifact.** Thứ tự cứng: ghi `.tmp` → `fs.rename` → **rồi mới** commit transaction. Ngược lại sinh hàng DB trỏ tới file không tồn tại |
| `lane.registry.ts` | `Record<Stage, Lane>` | Type-level ⇒ **thiếu một stage là lỗi biên dịch**. `lane-registry-complete` **không còn là test** |
| `lane.runner.ts` | `OnApplicationBootstrap`, mở N vòng lặp/lane | **Không dùng `@Interval`.** Lane là vòng lặp có backoff khi rỗng, không phải tick cố định |
| `graceful.ts` | `OnApplicationShutdown` | Dừng claim mới → chờ item đang chạy xong → **nhả lease**. Thiếu bước này thì mỗi lần deploy để lại việc treo 15 phút chờ reaper |
| `reaper.ts` | `@Cron` mỗi 60s | Lease hết hạn → `ready` |
| `gpu.semaphore.ts` | Redis semaphore **dùng chung** | Hai lane concurrency 1 mỗi lane **vẫn là 2 request đồng thời** tới một endpoint GPU. Một semaphore cho mọi lane gọi OmniVoice |

### Lease renewal cho job dài

`dub.lane` poll OmniVoice có thể chạy vài phút. `lane.base` phải **renew lease trong lúc `handle()` đang chạy**, không phải chỉ lấy lease lúc claim — nếu không reaper sẽ giật item khỏi lane đang xử lý nó.

---

## 2.7 `libs/ports/` — ba interface

```ts
export interface VideoDistributor {
  readonly name: string
  pick(rendition: Rendition, pages: Fanpage[]): Promise<Fanpage>
}

export interface Storage {
  pathFor(kind: AssetKind, key: string): string      // ffmpeg dùng TRỰC TIẾP
  put(kind: AssetKind, key: string, src: string): Promise<string>
  createReadStream(kind: AssetKind, key: string): Readable
  signedUrl(kind: AssetKind, key: string, ttlSec: number): Promise<string>
}

export interface TokenProvider {
  tokenFor(page: Fanpage): Promise<string>
  sync(): Promise<Fanpage[]>
}
```

| Interface | Impl hôm nay | Impl sau (không sửa caller) |
|---|---|---|
| `VideoDistributor` | `RoundRobinDistributor` | `TalentBoundDistributor` · `WeightedDistributor` · `ManualDistributor` |
| `Storage` | `LocalDiskStorage` | `S3Storage` khi tách máy GPU |
| `TokenProvider` | `ExchangeTokenProvider` | `PastedPageTokenProvider` · `MeAccountsOAuthProvider` |

Bind bằng NestJS DI theo env (`DISTRIBUTOR=round_robin`) ⇒ đổi implementation là đổi biến môi trường.

### `pathFor` cần docstring cảnh báo

Đây là thứ **duy nhất** `S3Storage` không làm được nguyên bản — nó sẽ phải download-vào-tmp-rồi-trả-path. Mọi lane ffmpeg đi qua `pathFor`, nên một implementation tương lai sẽ **âm thầm chậm đi 10 lần** nếu không ai biết trước. Viết điều này vào docstring, không vào wiki.

### Layout ổ đĩa

```
/data/source/{sec_user_id}/{aweme_id}.mp4
/data/dub/{rendition_id}.wav
/data/output/{rendition_id}.mp4
```

---

## 2.8 `Makefile`

`up` · `migrate` · `test` · `lint` · `patch-check` · `seed` · `retry-publish` · `check-tokens` · `disk-report`

---

## 2.9 Tests

**Hạ tầng (testcontainers Postgres — `SKIP LOCKED` không mock được):**

| Test | Kiểm |
|---|---|
| `claim-concurrency` | N lane cùng claim → không item nào bị hai lane lấy |
| `resume-after-crash` | Kill worker giữa `handle()` → reaper trả về `ready`, không mất, không trùng |
| `graceful-shutdown-releases-lease` | SIGTERM → item đang chạy xong, lease nhả, không chờ reaper |
| `advance-atomic-with-artifact` | Ném lỗi giữa ghi file và commit → không có hàng DB trỏ file không tồn tại |
| `secrets-never-logged` | Cookie · page token · app secret đi qua logger → không xuất hiện trong output |
| `module-isolation` | Nạp tay một `rendition` ở `approved` → Module 3 chạy được, không cần Module 2 |

**Thuần:** `crypto-roundtrip` · `storage-contract` (một bộ test chạy cho **mọi** impl `Storage`) · `errors-classification`

---

## Definition of done

- [ ] `make up` dựng đủ 6 service, không có minio
- [ ] `make migrate` chạy sạch trên DB rỗng, seed `vi`
- [ ] Bỏ một stage khỏi registry → **lỗi biên dịch**, không phải test đỏ
- [ ] 6 test hạ tầng xanh
- [ ] ESLint chặn được `libs/publish` import `libs/dubbing`
- [ ] Thiếu `ENCRYPTION_KEY` → crash lúc bootstrap, message rõ
