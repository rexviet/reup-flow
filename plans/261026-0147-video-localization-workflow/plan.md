---
title: "Video Localization Workflow — Temporal + AI agents"
description: "Mỗi video tải xong được một Temporal workflow biến thành mp4 lồng tiếng Việt, có sub Việt đè sub Trung, lưu trên object storage, trace và eval trên Langfuse."
status: pending
priority: P1
effort: 18d
branch: main
tags: [feature, backend, infra, ai]
blockedBy: []
blocks: []
created: 2026-10-26
brainstorm: plans/brainstorm/261026-0750-video-localization-workflow-brainstorm.md
diagram: https://claude.ai/artifact/AERLctbPRNFbUJ5cfhHoFr
---

# Implementation Plan — Video Localization Workflow

**Nguồn:**
- Brainstorm `261026-0750` (đã accepted, có diagram).
- Red-team ngày 2026-10-26, áp dụng toàn bộ (xem §8).
- Bản brainstorm dubbing `260827-2226` đã bị thay thế.

**Stack đã chốt:**
- Temporal tự host trong `docker-compose`.
- Service mới `services/localization`: Python, `pydantic-ai`.
- Service mới `services/renderer`: Node, Remotion (license miễn phí, ≤3 người) và Temporal TS worker.
- LLM và vision OCR gọi OpenRouter, OpenAI-compatible.
- STT và TTS gọi VoiceStudio ở máy khác, OpenAI-compatible, qua `VOICESTUDIO_URL` dạng https hoặc IP mạng riêng.
- Object storage dùng S3 API: MinIO ở local, S3 hoặc R2 ở production.
- Langfuse v3 tự host, kèm ClickHouse.
- Một instance Postgres, mỗi hệ có database và role riêng.

## Overview

**Khởi động workflow.**
- Downloader ghi một dòng vào `localization_outbox` trong cùng transaction đánh dấu video `done`.
- Starter Python đọc outbox, start workflow `video-{aweme_id}-g{generation}`, và inject
  `targetLang` cùng `voice` (đọc từ `.env`) vào input của workflow.

**Pipeline.**

| # | Bước | Loại | Task queue |
|---|---|---|---|
| 1 | ingest | activity | `media` |
| 2a ‖ 2b | transcribe ‖ detect_subtitles | activity | `stt` ‖ `io` |
| 3 | Localizer | agent (`TemporalAgent`) | `llm` (tool I/O → `io`) |
| 4 | synthesize_all | activity | `tts` |
| 5 | Dubber | agent (`TemporalAgent`) | `llm` (tool TTS → `tts`) |
| 6 ‖ 4b | assemble_audio ‖ layout_captions | activity, chạy sau bước 5 | `media` |
| 7 | render | activity, worker Node | `render` |
| 8 | Reviewer | agent thường, chạy **trong một activity** | `llm` |
| 9 | publish | activity | `io` |

Workflow chạy trên task queue `workflows`, do một worker riêng phục vụ. Worker này không phụ
thuộc VoiceStudio có đang sống hay không.

## Goals

| # | Goal | Priority |
|---|------|----------|
| 1 | Mỗi video tải xong tự ra `reup-media/output/{aweme_id}.mp4`, có lồng tiếng Việt và sub Việt | P1 |
| 2 | Workflow durable: crash ở đâu thì resume ở đó, không gọi lại provider cho việc đã xong | P1 |
| 3 | Provider (STT, TTS, OCR, LLM, storage) chỉ là cấu hình, không phải code | P1 |
| 4 | Mọi lần chạy đều trace được trên Langfuse, và có eval để so prompt hoặc model | P2 |

---

## 0. Sáu nguyên tắc bất khả xâm phạm

Nếu một phase làm bạn muốn vi phạm, **dừng lại và sửa plan trước**.

### 1. Code workflow tuyệt đối deterministic

- Không I/O, không đọc env, không đọc giờ hệ thống, không `random`, không gọi provider.
- Workflow **không được tự ghi S3**. Mọi lần ghi đi qua activity `persist_*`.

Cách enforce:
- Giữ sandbox của Temporal.
- Test kiến trúc quét AST với một allowlist đầy đủ (phase 03).
- **Replay test** trên history đã lưu sau mỗi phase. Thay đổi workflow mà không có
  `workflow.patched()` phải làm replay đỏ (phase 04).

### 2. Bytes không bao giờ đi qua payload Temporal

- Payload chỉ mang object key.
- Guard được làm bằng `PayloadCodec`, vì `PydanticAIPlugin` thay mất `PayloadConverter` tuỳ
  biến nhưng giữ lại codec. Payload vượt 256KB thì fail ngay.
- Guard được test qua một `Client` thật có gắn plugin.
- Ảnh của Reviewer không bao giờ rời khỏi activity của nó.
- **Cấm presigned URL** trong payload, prompt và span.

### 3. Secret không bao giờ nằm trong input, history, span hay stdout

- `VoiceRef.settings` là model đóng (`extra="forbid"`). Tên field nào giống
  `key|token|secret|password` thì bị từ chối.
- Test canary chạy qua **class provider thật** gọi vào một stub HTTP, gồm cả nhánh lỗi
  401/500/timeout. Test quét history (kể cả failure payload), span và stdout/stderr.

### 4. Activity có side effect bên ngoài phải idempotent theo key xác định

| Side effect | Key xác định |
|---|---|
| Cache TTS | `sha256(canonical{provider, model, engine, voice_id, settings, text_vi})`, nằm ở `cache/tts/` (không có lifecycle) |
| Artifact theo vòng review | `work/{aweme_id}/r{round}/...` |
| Render | `render-{spec_sha256}.mp4` |
| Glossary | `ON CONFLICT DO NOTHING` |
| Outbox | Khoá theo `aweme_id`, có `generation` |

Bằng chứng là test **đếm số lời gọi theo từng hash**, gồm cả trường hợp kill giữa lúc provider
trả lời và lúc ghi S3.

### 5. Chỉ có một bản dịch

- Captioner, assemble và Reviewer chỉ đọc `LocalizedScript` **phiên bản mới nhất**.
- Mọi đường chạy lại sau Dubber đều chạy lại cả 4b.
- `EditSpec` mang `script_version` và `audio_sha256`.
- Có test kiến trúc kiểm chứng điều này. Có test so text sub với metadata `text_vi` của object TTS.

### 6. Guard nào cũng có mutation test, được đăng ký vào `scripts/run-mutations.sh` trong cùng commit

- Runner được refactor ở phase 03 để chạy harness Python và Node.
- Fingerprint phủ các đuôi `.py`, `.tsx`, `.json`, `.ttf`.
- Mọi harness dùng chung protocol output qua `scripts/lib/mutation-protocol.sh`.

---

## 1. Trạng thái xuất phát

**Đã có:**
- `services/downloader` (NestJS).
- `markDone` (`services/downloader/src/db/videos.repository.ts:237`), nhận một `Executor` nên
  dùng được trong transaction. Nó được gọi ở `services/downloader/src/lanes/download.lane.ts:456`.
- Interface `Storage`, hiện chỉ có `LocalDiskStorage`, có registry in-flight và sweep theo mtime.
- Migration là raw SQL với **tên bảng không kèm schema**: production nằm ở `public`, còn test
  dùng schema riêng cho mỗi worker (`services/downloader/test/support/db.ts:55-72`).
- `scripts/run-mutations.sh` có `HARNESS_DIR` ghi cứng cho downloader, và fingerprint chỉ phủ
  `*.ts`, `*.sql`, `*.yml`.

**Chưa có:** Temporal, MinIO, Langfuse, ClickHouse, `services/localization`, `services/renderer`.

**Thiếu input từ người dùng:** `VOICESTUDIO_URL`. Đây là input của gate G2.

**Đã verify bằng code của VoiceStudio** (`6d0227c`):
- Có `POST /v1/audio/transcriptions` (với `verbose_json`, word timestamps), `POST /v1/audio/speech`,
  và `GET /v1/audio/voices`.
- Auth bằng `Bearer`, **chỉ khi server có đặt `OMNIVOICE_API_KEY`**. Client từ loopback hoặc
  trusted network được bỏ qua kiểm tra (G2 kiểm phải ra 401).
- Với tiếng Việt, VoiceStudio **không đổi số thành chữ**.
- Nếu `voice_id` không khớp profile nào, nó âm thầm rơi về preset của engine.

---

## 2. Những quyết định phải nằm đúng chỗ

| ID | Nội dung | Phase |
|---|---|---|
| D1 | Outbox là bảng **không kèm schema** `localization_outbox`, nằm cạnh `videos`, ghi cùng transaction với `markDone` | 02 |
| D2 | Starter viết bằng Python. Downloader không import Temporal (có rule ESLint) | 02, 04 |
| D3 | S3 commit tại `CompleteMultipartUpload`. `beforeCommit` chạy ngay trước đó. Từ chối thì `Abort` | 01 |
| D4 | Sweep S3 abort các upload dở có tuổi > `S3_STALE_UPLOAD_MS` (mặc định 24h), không dùng lease 5 phút | 01 |
| D5 | Postgres init bằng service `db-init` idempotent. Mỗi hệ có role riêng không phải superuser. `REVOKE CONNECT ON DATABASE reup FROM PUBLIC` | 00 |
| D6 | `estimate_duration` đếm âm tiết **sau khi** đổi số sang chữ và ước lượng từ Latin | 05 |
| D7 | Font sub là một file, đường dẫn đọc từ **một** config. Kiểm bằng hành vi: đo chữ so với pixel thật khi render | 07 |
| D8 | `EditSpec` sinh JSON Schema từ Pydantic. Mutation drift nằm ở harness Python | 03, 07 |
| D9 | Workflow id `video-{aweme_id}-g{generation}`, reuse policy `REJECT_DUPLICATE`. Chạy lại bằng cách tăng `generation` | 02, 04 |
| D10 | Thiếu artifact (`NoSuchKey`) là lỗi non-retryable. Workflow chạy lại stage sinh ra artifact đó đúng một lần | 03, 04 |
| D11 | Reviewer là agent pydantic-ai thường, chạy **bên trong một activity** `review_render` | 08 |
| D12 | Renderer phục vụ file tạm qua HTTP server localhost cho Remotion | 07 |

---

## 3. Một gate cứng

```
Phase 00 ──GATE──> Phase 01..10
```

| Gate | Câu hỏi | Nếu FAIL |
|---|---|---|
| G1 | `TemporalAgent` + OpenRouter resume được không? Tool không bị gọi lại? Tool có ghim được `task_queue` riêng không? | Bỏ `TemporalAgent`, mỗi lượt agent chạy trong activity có checkpoint thủ công |
| G2 | STT của VoiceStudio có timestamp mức từ đủ dùng không (tính cả tỷ lệ từ thiếu timestamp, đo đúng cách)? TTS ra audio đo được không? `voice_id` có tồn tại không? Thiếu key có bị 401 không? | Đổi `SttProvider` sang API cloud. Không có 401 thì không chạy tiếp cho tới khi server bật auth |
| G3 | Một run có ra **đúng một trace**, có cả span của worker Node, và số span không đổi sau khi kill rồi resume? | Gắn trace id thủ công |
| G4 | Temporal và Langfuse chạy bằng role riêng. Role `langfuse` bị từ chối khi `\c reup`. Schema `reup` không đổi. `count_workflows` chạy được trên SQL visibility. Không service mới nào bind `0.0.0.0` | Dừng lại hỏi người dùng |
| G5 | Vision LLM trả bbox dải sub đạt IoU trung vị ≥ 0.7 **và** p10 ≥ 0.5 trên 20 frame? | Viết implementation OCR thứ hai (OCR chuyên dụng) |
| G6 | Kích thước input lớn nhất của các activity model-request của Localizer, trên video 3 phút, có < 200KB không? | Localizer chia theo cửa sổ 60 giây |

---

> **Trạng thái 2026-10-26:** phase 00 `in-progress`. Hạ tầng xong; G1, G2, G3, G4, G6 đạt (G2 chấp nhận ở mức 9/10 theo quyết định của người dùng); G5 chưa chạy (thiếu nhãn bbox). Phase 01–03 đã làm xong. Phase 04 `in-progress`: code và test xong, còn bước thủ công cần cấu hình provider, áp migration lên `reup` và ghi lại histories replay từ provider thật (xem phần Tiến độ của nó). Phase 05 `in-progress`: code, test và mutation xong; chưa chạy được video thật (VoiceStudio không với tới, chưa có `VOICE_ID`/`STT_MODEL`/`TTS_MODEL`), nên hiệu chỉnh tốc độ đọc và baseline cho phase 09 chưa có (xem phần Tiến độ của nó). Phase 06 `in-progress`: code, test, replay và mutation xong (AC1 kiểm bằng kill thật, AC6 gồm cả TTS của Dubber); chưa chạy được với giọng và model thật, nên LUFS và tỷ lệ segment không vừa slot chưa đo trên dữ liệu thật (xem phần Tiến độ của nó). G5 vẫn tạm bỏ qua. Xem phần "Tiến độ" trong [phase-00](./phase-00-infra-and-spike-gate.md) để biết việc còn lại và cách quay lại.

## 4. Phase index

| # | File | Nội dung | Effort | Blocked by | Milestone |
|---|---|---|---|---|---|
| 00 | [phase-00-infra-and-spike-gate.md](./phase-00-infra-and-spike-gate.md) | Compose bảo mật (port, role, ACL, digest) · 6 gate G1–G6 | ~3d | — | **GATE** |
| 01 | [phase-01-s3-storage.md](./phase-01-s3-storage.md) | `S3Storage` (D3, D4) · migrate file local có `--revert` | ~1d | 00 | M1 |
| 02 | [phase-02-transactional-outbox.md](./phase-02-transactional-outbox.md) | `localization_outbox` không kèm schema · có `generation` · backfill | ~0.5d | 00 | M1 |
| 03 | [phase-03-localization-foundation.md](./phase-03-localization-foundation.md) | Contract đầy đủ · provider · codec guard · canary · test kiến trúc · **refactor mutation runner** | ~2d | 00 | M1 |
| 04 | [phase-04-starter-and-prep-activities.md](./phase-04-starter-and-prep-activities.md) | Starter (backoff, kiểm điều kiện, throttle) · worker theo queue · replay test · tracing · bước 1, 2a, 2b | ~2.5d | 01, 02, 03 | **M2** |
| 05 | [phase-05-localizer-agent.md](./phase-05-localizer-agent.md) | Localizer · glossary có rào chắn · `estimate_duration` (D6) | ~1.5d | 04 | M3 |
| 06 | [phase-06-dubbing.md](./phase-06-dubbing.md) | Cache TTS theo hash (khử trùng, không lifecycle) · Dubber · assemble | ~2d | 05 | M3 |
| 07 | [phase-07-captions-and-renderer.md](./phase-07-captions-and-renderer.md) | layout_captions · renderer (HTTP localhost, OTel TS) · contract và font parity | ~2d | 03, 04 | M3 |
| 08 | [phase-08-reviewer-and-publish.md](./phase-08-reviewer-and-publish.md) | Reviewer trong activity · định tuyến chạy lại đúng · `review/` · publish · nối phase 06 với 07 | ~1.5d | 06, 07 | **M4** |
| 09 | [phase-09-observability-and-eval.md](./phase-09-observability-and-eval.md) | Score · dataset · evaluator `fit_ratio`, `fidelity`, `naturalness` | ~1d | 08 | M5 |
| 10 | [phase-10-e2e-and-docs.md](./phase-10-e2e-and-docs.md) | Acceptance AC1–AC8 trên stack thật · backfill · docs và runbook | ~1d | 09 | **M5** |

**Tổng khoảng 18 ngày code.**

Phase 07 làm renderer và layout bằng fixture, nên song song được với 05–06. Phase 08 nối thật
hai nhánh lại với nhau.

---

## 5. Bảng mutation test bắt buộc

| Invariant | Phase | Mutation | Kỳ vọng |
|---|---|---|---|
| S3 decline thì không còn object hay upload dở | 01 | Bỏ `Abort` | `ListMultipartUploads` khác rỗng → **đỏ** |
| `beforeCommit` chạy trước commit | 01 | Gọi sau `Complete` | **Đỏ** |
| Stream bị cắt thì không commit | 01 | Bỏ kiểm số byte | **Đỏ** |
| Sweep không abort upload của process khác | 01 | Dùng lease thay cho `S3_STALE_UPLOAD_MS` | Test hai process **đỏ** |
| Outbox nguyên tử với `done` | 02 | INSERT nằm ngoài transaction | **Đỏ** |
| Workflow deterministic | 03 | `import os` trong `workflows/` | **Đỏ** |
| Codec guard còn hiệu lực khi có plugin | 03 | Bỏ codec, **hoặc** gắn guard như converter | Test qua `Client` thật **đỏ** |
| Secret canary | 03 | `VoiceRef` mang giá trị key đọc từ settings | **Đỏ**. Control: canary trong failure payload bị bắt |
| Validate `aweme_id` | 03 | Bỏ regex | Fixture path traversal **đỏ** |
| Một workflow cho mỗi generation | 04 | Bỏ id xác định | **Đỏ** |
| Chạy lại `needs_human` được | 04 | Không tăng `generation` | **Đỏ** |
| Concurrency theo queue (`stt`, `tts`, `media`, `render`) | 04, 06, 07 | Bỏ giới hạn | Test overlap **đỏ**. Control: đặt giới hạn 3 thì thấy chồng nhau |
| Replay an toàn | 04 | Đảo thứ tự activity mà không `patched()` | Replay **đỏ** |
| Localizer phủ đủ segment | 05 | Bỏ validator | **Đỏ** |
| Glossary không bị ghi đè hay chèn URL | 05 | `DO UPDATE` / bỏ chặn URL | Fixture injection **đỏ** |
| TTS gọi ≤ 1 lần mỗi hash | 06 | Bỏ cache, hoặc bỏ khử trùng | **Đỏ** |
| TTS của Dubber đi queue `tts` | 06 | Bỏ ghim queue | Test overlap **đỏ** |
| Một bản dịch | 06, 07, 08 | Chạy lại `dub` mà không chạy 4b | Test so text sub với text TTS **đỏ** |
| Contract EditSpec | 07 | Sửa model mà không sinh lại schema | **Đỏ** (harness Python) |
| Font đo = font vẽ | 07 | Đổi config font ở một phía | Test đo pixel **đỏ** |
| Retry đúng stage | 08 | Chạy lại toàn bộ khi lỗi `dub` | **Đỏ** |
| `needs_human` không publish | 08 | Publish khi fail 2 vòng | `output/` có object → **đỏ** |

---

## 6. Hợp đồng biến env

Phase nào thêm biến thì cập nhật `.env.example` và allowlist của service trong cùng commit.
`services/downloader/test/compose-constraints.spec.ts` được mở rộng để kiểm từng service chỉ
nhận đúng các biến của nó.

| Biến | Service | Mặc định | Phase |
|---|---|---|---|
| `STORAGE_BACKEND`, `S3_ENDPOINT`, `S3_REGION`, `S3_BUCKET`, `S3_FORCE_PATH_STYLE`, `S3_KEY_PREFIX` (bắt buộc, khác rỗng), `S3_STALE_UPLOAD_MS` | nest | `local`, —, `us-east-1`, `reup-media`, `true`, `source/`, `86400000` | 01 |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` (key của downloader, chỉ có quyền trên `source/*`) | nest | — | 01 |
| `LOC_S3_*` (key của localization: đọc `source/*`, đọc ghi `work/*`, `cache/*`, `review/*`, chỉ ghi `output/*`) | worker-*, starter | — | 03 |
| `RENDER_S3_*` (chỉ trên `work/*`) | worker-render | — | 07 |
| `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` | worker-*, starter | `temporal:7233`, `default` | 00 |
| `LOC_DATABASE_URL` (role `localization`) | worker-agents, starter | — | 03 |
| `OPENROUTER_BASE_URL`, `OPENROUTER_API_KEY` | worker-agents | `https://openrouter.ai/api/v1`, — | 03 |
| `LOCALIZER_MODEL`, `DUBBER_MODEL`, `REVIEWER_MODEL`, `OCR_MODEL`, `OCR_BBOX_FORMAT`, `JUDGE_MODEL` | worker-agents | — | 03, 09 |
| `VOICESTUDIO_URL` (https hoặc IP mạng riêng), `VOICESTUDIO_API_KEY`, `VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS` | worker-agents, **starter** (starter dùng để kiểm `VOICE_ID` có trong `GET /v1/audio/voices` trước khi dispatch; không dùng khi `VOICE_PROVIDER` khác `voicestudio`) | —, —, `100.64.0.0/10` | 03, 04 |
| `TTS_MODEL`, `STT_MODEL` | worker-agents | — | 03 |
| `TARGET_LANG`, `VOICE_PROVIDER`, `VOICE_ID`, `VOICE_SETTINGS_JSON` | starter | `vi`, `voicestudio`, —, `{}` | 04 |
| `STT_CONCURRENCY`, `TTS_CONCURRENCY`, `MEDIA_CONCURRENCY`, `RENDER_CONCURRENCY`, `LLM_CONCURRENCY`, `IO_CONCURRENCY` | worker-* | `1`, `2`, `2`, `1`, `8`, `2` | 04, 07 |
| `MAX_OPEN_WORKFLOWS`, `OUTBOX_MAX_ATTEMPTS` | starter | `10`, `5` | 04 |
| `LOUDNESS_LUFS`, `TRUE_PEAK_DBTP` | worker-media | `-14`, `-1.5` | 06 |
| `CAPTION_FONT_PATH`, `COVER_COLOR`, `COVER_OPACITY` | worker-media, worker-render | `assets/fonts/BeVietnamPro-SemiBold.ttf`, `#101010`, `0.92` | 07 |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | worker-*, starter | — | 04 |

**Ràng buộc GPU:** `STT_CONCURRENCY + TTS_CONCURRENCY ≤` số GPU worker của VoiceStudio.

---

## 7. Rủi ro

| Rủi ro | Phase | Xử lý |
|---|---|---|
| Tích hợp Temporal của pydantic-ai đang beta | 00, 05 | Pin version. G1 và G6 là gate. Agent gom trong `agents/` |
| VoiceStudio một GPU, backfill dồn hàng | 04 | Concurrency theo queue. Starter giới hạn `MAX_OPEN_WORKFLOWS`. Lỗi 429/504 là tạm thời, có backoff |
| Workflow sống lâu hơn lifecycle của `work/` | 00, 04 | Lifecycle `work/` là 30 ngày. Workflow có `execution_timeout` 14 ngày. Thiếu artifact thì chạy lại stage sinh ra nó (D10) |
| Prompt injection từ chữ trên màn hình hoặc lời thoại | 05, 08 | Nội dung nằm trong khối dữ liệu có ranh giới rõ. Glossary không ghi đè. Chặn URL và tên miền → `needs_human` |
| ClickHouse tốn 2–4GB RAM, cộng Demucs và Chromium | 00, 04, 07 | Giới hạn memory trong compose. `MEDIA_CONCURRENCY` và `RENDER_CONCURRENCY` |
| MinIO community đã bị archive | 00 | Chỉ dùng ở local, pin digest (ưu tiên bản Chainguard). Code chỉ dùng S3 API |
| Image hoặc weights bị thay ở upstream | 00, 04 | Pin bằng digest. Weights Demucs có sha256. `torch.load(weights_only=True)` |

---

## 8. Lệch có chủ ý so với brainstorm

| Brainstorm nói | Plan làm | Lý do |
|---|---|---|
| "outbox/reconciler" | Outbox cùng transaction, kèm backfill một lần | Không có khe nào làm mất trigger |
| Không nói ai start workflow | Starter Python | Downloader giữ "Postgres là kênh duy nhất" |
| Diagram vẽ 4b song song với 4a | 4b chạy sau Dubber, song song với bước 6 | Dubber có thể viết lại câu (nguyên tắc 5) |
| Reviewer là agent như hai agent kia | Reviewer chạy trong một activity (D11) | Ảnh sẽ đi vào message history của `TemporalAgent` và vượt giới hạn payload |
| Lifecycle `work/` 7 ngày | 30 ngày. Cache TTS và `review/` không có lifecycle | Workflow có thể chờ VoiceStudio nhiều ngày khi backfill |
| Evaluator: độ trung thành và độ tự nhiên | `fit_ratio` + `fidelity` + `naturalness` | `fit_ratio` deterministic và gần như miễn phí. Bỏ `glossary_consistency` vì không ai yêu cầu |
| STT spike gồm VoiceStudio hai engine và một API cloud | Có cloud candidate **nếu có key** | Không có key thì G2 ghi rõ là đã bỏ qua |

---

## Success Criteria

- [ ] **AC1:** mỗi hash TTS chỉ gọi provider tối đa một lần. Ngoại lệ là các hash đang chạy dở
  đúng lúc kill. Số ngoại lệ không vượt `TTS_CONCURRENCY` và phải được liệt kê từng cái. Lời gọi
  hiệu chỉnh tốc độ đọc và các lần Dubber viết lại câu có hash riêng (phase 06, kiểm lại ở 10).
- [ ] **AC2:** text sub trong EditSpec khớp với metadata `text_vi` của các object TTS dùng trong
  `DubbedAudio`, kể cả sau vòng chạy lại (phase 08).
- [ ] **AC3:** mỗi `spec_sha256` chỉ gọi `renderMedia` đúng một lần, đếm bằng counter phía
  renderer, kể cả với run có vòng review. Output nằm ở `output/{aweme_id}.mp4` (phase 08).
- [ ] **AC4:** trigger trùng chỉ chạy một workflow mỗi generation. Video `done` luôn có dòng
  outbox. Chạy lại một video `needs_human` được bằng cách tăng generation (phase 02, 04).
- [ ] **AC5:** đổi voice trong `.env` chỉ áp cho workflow mới (phase 04).
- [ ] **AC6:** các queue `stt`, `tts`, `media`, `render` không bao giờ vượt concurrency đã cấu
  hình, kể cả TTS do Dubber gọi (phase 04, 06, 07, 10).
- [ ] **AC7:** mỗi run là một trace trên Langfuse, có span của LLM, STT, OCR, TTS **và
  `render`**, cùng score của Reviewer. Kiểm qua API của Langfuse (phase 09, 10).
- [ ] **AC8:** một experiment trên dataset Localizer ra `fit_ratio`, `fidelity` và `naturalness`,
  so sánh được giữa hai phiên bản (phase 09).
- [ ] `scripts/run-mutations.sh` chạy đủ mọi harness, đúng số lượng đã khai, và cây code không đổi sau khi chạy.

## Red Team Review

### Session — 2026-10-26

**Findings:** 39 finding thô từ 4 reviewer. Sau khi gộp trùng còn 28, chấp nhận cả 28 (2 trong
số đó có điều chỉnh, 0 bị từ chối).

**Mức độ:** 8 Critical, 9 High, 11 Medium.

| # | Finding | Severity | Disposition | Applied To |
|---|---|---|---|---|
| 1 | Chạy lại `dub` không dựng lại sub. Render bỏ qua vì spec không đổi | Critical | Accept | §0.5, 06, 08, AC2 |
| 2 | Không chạy lại được video (reuse policy, `needs_human` là completed) | Critical | Accept | D9, 02, 04, AC4 |
| 3 | Lifecycle 7 ngày xoá artifact đang dùng. `NoSuchKey` retry mãi | Critical | Accept | D10, 00, 04, 06, 08 |
| 4 | Ảnh Reviewer đi vào history của `TemporalAgent`. Presigned URL không gọi tới được và gây lộ | Critical | Accept | D11, G6, 08 |
| 5 | `PydanticAIPlugin` thay mất payload guard dạng converter | Critical | Accept | §0.2, 03 |
| 6 | Outbox ghi cứng schema làm vỡ test cô lập. Rollback `CASCADE` nguy hiểm | Critical | Accept | D1, 02 |
| 7 | Mutation runner không chạy hay fingerprint được harness mới | Critical | Accept | §0.6, 03, 10 |
| 8 | Service mới không có quy định port hay auth. Dùng chung superuser và Redis chứa cookie | Critical | Accept | D5, 00, §6 |
| 9 | Không có versioning hay replay | High | Accept | §0.1, 04 |
| 10 | Lỗ hổng concurrency. Queue workflow phụ thuộc health check của VoiceStudio | High | Accept | 04, 06, 07, AC6 |
| 11 | Contract chưa định nghĩa. Allowlist mâu thuẫn. Workflow tự ghi S3 | High | Accept | 03, 05, 06 |
| 12 | Starter bị chặn đầu hàng. Không kiểm điều kiện. Không validate `aweme_id` | High | Accept | 03, 04 |
| 13 | Prompt injection làm bẩn glossary | High | Accept (điều chỉnh: tự dùng có rào chắn, không cần duyệt) | 03, 05 |
| 14 | Canary không chạm tới đường rò rỉ thật | High | Accept | §0.3, 03 |
| 15 | Renderer: file local, ghi hash không nguyên tử, thiếu OTel ở Node | High | Accept | D12, 07, AC7 |
| 16 | Sweep S3 abort nhầm upload của process khác | High | Accept | D4, 01 |
| 17 | AC1 viết sai. Test kill chỉ ở thời điểm an toàn | High | Accept | §0.4, 06, AC1 |
| 18–28 | Thiếu model/engine trong cache key · rollback phase 01 bằng restore cả DB · đếm âm tiết số và từ Latin · chỉ số G2 không đo được gì · `voice_id` không tồn tại · quy ước bbox · pin digest và checksum weights · hợp đồng env · font parity · cách kiểm AC2/3/5 · lệch so với brainstorm chưa liệt kê | Medium | Accept (#28: cloud STT chỉ khi có key) | 00–10, §6, §8 |

### Whole-Plan Consistency Sweep

Các thay đổi đã đồng bộ khắp plan:
- Đổi tên `localization.outbox` thành `localization_outbox`.
- Workflow id có thêm hậu tố `-g{generation}`.
- Cache TTS chuyển sang `cache/tts/`.
- Thêm `review/`.
- Reviewer chạy trong activity.
- Render dùng key `render-{hash}`.
- Pipeline, AC, §5 và §6 đã khớp với từng phase.

Còn mở: G2 cần `VOICESTUDIO_URL` (input, không phải mâu thuẫn).

## Validation Log

### Session 1 — 2026-10-26

**Verification Results:**
- Claims checked: ~40, bởi reviewer Fact Checker và Flow Tracer, cùng mình tự kiểm `db.ts`,
  migration, fingerprint và port compose.
- Verified: 38.
- Failed: 0.
- Corrected: 2. "Auth Bearer" phải đi kèm `OMNIVOICE_API_KEY`. `HARNESS_DIR` ghi cứng.
- Tier: Full.

**Quyết định:**

| Câu hỏi | Trả lời | Ảnh hưởng |
|---|---|---|
| Áp dụng red-team | Toàn bộ | Mọi phase |
| License Remotion | Cá nhân hoặc ≤3 người, miễn phí | Giữ phase 07 dùng Remotion |
| Glossary do agent đề xuất | Tự dùng, có rào chắn, không cần duyệt | Phase 05 |
| OCR dự phòng | Chỉ làm nếu G5 fail | Phase 00, 03 |

**Còn mở:** `VOICESTUDIO_URL` (blocker riêng của G2).

<!-- slug: video-localization-workflow -->
