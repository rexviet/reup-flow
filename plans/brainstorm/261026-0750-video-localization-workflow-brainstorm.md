---
type: brainstorm-summary
title: "Video Localization Workflow — brainstorm summary"
date: 2026-10-26
status: accepted
source: conversation
supersedes: 260827-2226-dubbing-service-brainstorm.md
next: plan
---

# Video Localization Workflow — Brainstorm Summary

**Ngày:** 2026-10-26. Nguồn: hội thoại brainstorm, code trong repo, và docs của Pydantic AI,
Temporal, Langfuse, MinIO.

## 1. Bối cảnh

Hiện tại `services/downloader` (NestJS) đã tải được video Douyin:
- Các lane chạy trên Postgres với `SKIP LOCKED`.
- File được lưu vào `DATA_DIR` qua interface `Storage`
  (`services/downloader/src/storage/storage.interface.ts`). Hiện interface này chỉ có
  implementation local disk.
- Các bảng nằm trong schema `public` của database `reup`.

Mục tiêu mới là biến mỗi video đã tải thành một **Temporal workflow durable**, trong đó AI agent
đảm nhận những bước cần phán đoán. Bản này thay thế bản brainstorm dubbing qua OmniVoice
ngày 2026-08-27.

## 2. Contract

### Outcome

Mỗi video tải xong được tự động biến thành một mp4 có lồng tiếng Việt và sub tiếng Việt đè lên
sub tiếng Trung, lưu trên object storage. Workflow chạy durable trên Temporal tự host. Mọi lần gọi
LLM và provider đều được trace trên Langfuse tự host, và Langfuse cũng là nơi chạy eval.

### Constraints

**Phạm vi**
- Downloader giữ nguyên và nằm ngoài workflow. Hai chế độ (backfill cả kênh và tải phần mới) đã có sẵn.

**Input của workflow**
- `targetLang` (cố định là `vi`) và `voice` được inject vào input của workflow.
- v1 lấy giá trị từ `.env` ở starter. Sau này sẽ lấy từ dashboard.
- Workflow không tự đọc env, vì đọc env trong workflow sẽ làm replay ra kết quả khác.
- `voice` chỉ là tham chiếu `{provider, voiceId, settings}`. API key nằm ở env của worker,
  không đi vào history của Temporal.
- Một voice dùng cho mọi người nói, nên không cần diarization.

**Provider và tầng tách provider**
- STT và TTS gọi qua HTTP tới VoiceStudio chạy trên một máy khác (xem 7.5). OCR và LLM đi qua
  OpenRouter. Máy chạy hệ thống này không cần GPU.
- Tách vocals/BGM bằng Demucs chạy trên CPU.
- STT, OCR, TTS, tách nguồn âm và LLM đều nằm sau interface độc lập với provider.
- Với LLM, tầng tách provider chính là model abstraction của pydantic-ai.

**Payload và trigger**
- File chỉ đi qua payload Temporal dưới dạng object key, không bao giờ đưa bytes vào payload.
- Workflow ID là `video-{aweme_id}`, nên mỗi video chỉ có một workflow.

**Hạ tầng tự host trong `docker-compose`**
- Temporal và Langfuse dùng chung Postgres với hệ thống.
- Langfuse và output video dùng chung object storage, tách theo bucket.

### Non-goals (v1)

- Upload lên nền tảng.
- Dashboard.
- Nhiều ngôn ngữ đích.
- Mỗi người nói một voice riêng.
- Inpainting vùng sub.
- CodeMode/Monty.
- Quản lý prompt trên Langfuse.
- Tinh chỉnh hiệu ứng hình ảnh nhằm né hệ thống phát hiện nội dung.

### Acceptance criteria

1. **Resume không gọi lại provider.** Kill worker giữa bước TTS rồi resume: các segment đã TTS
   không bị gọi lại provider. Kiểm chứng bằng cách đếm số lần gọi provider.
2. **Một nguồn bản dịch.** Sub và lời dub đều lấy từ cùng một `LocalizedScript`.
3. **Render một lần.** Mỗi EditSpec chỉ render đúng một lần. Output nằm ở
   `reup-media/output/{aweme_id}.mp4`.
4. **Chống trùng và chống mất trigger.**
   - Trigger cùng một video hai lần chỉ tạo một workflow.
   - Nếu crash giữa lúc mark `done` và start workflow, reconciler vẫn bắt được video đó.
5. **Config không lệch giữa chừng.** Đổi voice trong `.env` chỉ áp cho workflow mới.
6. **Concurrency có giới hạn.** Backfill không vượt giới hạn concurrency đã cấu hình cho từng task queue.
7. **Trace đầy đủ.** Mỗi workflow run tương ứng một trace trên Langfuse, trong đó đủ span của
   mọi lần gọi LLM, STT, OCR và TTS. Verdict của Reviewer được ghi thành score trên trace đó.
8. **Eval chạy được.** Một experiment trên dataset của Localizer chạy được và ra score so sánh
   được giữa hai phiên bản prompt hoặc model.

## 3. Pipeline

```
Downloader (TS) ── outbox/reconciler ──> start VideoLocalizationWorkflow
                                          id    = video-{aweme_id}
                                          input = LocalizationInput

VideoLocalizationWorkflow (Python)
  1. ingest             activity  [media]   ffprobe, tách wav, Demucs → vocals + bgm
  2. transcribe         activity  [io]      STT API trên vocals     ┐ chạy song song
  3. detect_subtitles   activity  [io]      lấy mẫu frame + OCR API ┘
  4. Localizer          AGENT     [llm]     → LocalizedScript
  5. synthesize_all     activity  [tts]     TTS song song từng segment (có cache)
  6. Dubber             AGENT     [llm]     chỉ chạy cho các segment bị tràn: rút gọn rồi TTS lại
  7. assemble_audio     activity  [media]   ghép clip, duck BGM, loudnorm → dubbed.wav
  8. layout_captions    activity  [media]   LocalizedScript + vùng sub → EditSpec
  9. render             activity  [render]  worker Node/Remotion → mp4
 10. Reviewer           AGENT     [llm]     → ReviewVerdict; fail thì chạy lại đúng stage, tối đa 2 vòng
 11. publish            activity  [io]      output/{aweme_id}.mp4 + ghi DB + score lên Langfuse
```

Ghi chú: bước 8 có thể chạy song song với các bước 5–7.

Chỉ có ba bước dùng LLM. Những bước còn lại là deterministic, nên không có lý do để LLM quyết
định có gọi STT hay không. "Dịch" không phải là một tool: dịch chính là output của Localizer.

## 4. Agent và tool

Tool nào cần I/O đều chạy thành activity riêng, theo cơ chế của `TemporalAgent`.

### 4.1 Localizer (agent)

- **Nhiệm vụ:** ghép STT với OCR, dịch zh→vi sao cho đọc vừa thời lượng của segment, và giữ
  thuật ngữ nhất quán trong cùng một kênh.
- **Input:** `Transcript`, `SubtitleTrack`, metadata của video (title, desc), `targetLang`, `channelId`.
- **Output:** `LocalizedScript`. Output validator kiểm tra mọi segment nguồn đều được phủ và
  giữ nguyên id. Nếu sai thì trả `ModelRetry` để agent làm lại.

| Tool | Loại | Mô tả |
|---|---|---|
| `read_transcript(from_ms, to_ms)` | đọc | Đọc transcript theo cửa sổ thời gian |
| `read_onscreen_text(from_ms, to_ms)` | đọc | Đọc cue OCR kèm bbox. Ưu tiên dùng cho tên riêng và từ Hán Việt |
| `glossary_lookup(terms_zh[])` | đọc DB | Tra glossary của kênh |
| `glossary_upsert(zh, vi, note)` | ghi DB | Thêm hoặc sửa thuật ngữ trong scope kênh. Idempotent |
| `estimate_duration(text_vi)` | tính | Deterministic: số âm tiết ÷ tốc độ đọc của voice (đo trước) → ms |

### 4.2 Dubber (agent, scope hẹp)

- **Nhiệm vụ:** chỉ xử lý các segment có thời lượng TTS vượt slot quá 15%. Các segment này
  thường chiếm khoảng 10–20%.
- **Phần còn lại không cần LLM.** TTS toàn bộ nằm ở bước 5 (deterministic). Các segment lệch
  ≤15% được time-stretch ở bước 7.
- **Output:** danh sách `{segment_id, text_vi_mới, audio_key, duration_ms}`.

| Tool | Loại | Mô tả |
|---|---|---|
| `get_segment(id)` | đọc | Câu gốc, bản dịch hiện tại, slot ms, segment trước/sau làm ngữ cảnh |
| `estimate_duration(text_vi)` | tính | Dùng chung với Localizer. Thử trước khi tốn tiền TTS |
| `synthesize(id, text_vi)` | TTS | Gọi `TtsProvider` với voice được inject. Cache theo `hash(text, voice)` |
| `accept(id, stretch_ratio)` | ghi | Chốt segment, `stretch_ratio` trong khoảng [0.85, 1.15] |

### 4.3 Reviewer (agent, multimodal)

- **Nhiệm vụ:** chấm mp4 đã render và gán mỗi lỗi vào đúng stage gây ra nó.
- **Output:** `ReviewVerdict { pass, scores{fidelity, timing, visual}, issues[{segment_id?, stage: localize|dub|caption, reason}] }`.
- **Workflow xử lý verdict:** chạy lại stage bị lỗi, tối đa 2 vòng. Quá 2 vòng thì đánh dấu `needs_human`.

| Tool | Loại | Mô tả |
|---|---|---|
| `alignment_report()` | tính | Deterministic: overflow % và stretch của từng segment, khoảng lặng, LUFS, true peak |
| `sample_frames(times_ms[])` | media | Lấy frame từ mp4 output đưa cho model xem: sub có đọc được không, có che kín sub Trung không |
| `residual_source_text(times_ms[])` | OCR | Kiểm tra khách quan xem còn sót chữ Trung nào ngoài vùng đã che không |
| `read_script(segment_ids[])` | đọc | So câu gốc với câu dịch |

### 4.4 Không phải agent

- **Captioner** là deterministic ở v1 (bước 8: `layout_captions`):
  - Ngắt dòng tiếng Việt theo độ rộng đo được của text.
  - Chọn cỡ font và hộp che phủ vùng sub.
  - Chỉ nâng lên agent khi việc layout thật sự cần phán đoán.
- **Renderer** là deterministic.

### 4.5 Interface provider (Python `Protocol`)

| Interface | Phương thức |
|---|---|
| `SttProvider` | `transcribe(audio_key, lang) -> Transcript` (cần có timestamp) |
| `OcrProvider` | `read(frame_keys[]) -> list[OcrCue]` (có bbox) |
| `TtsProvider` | `synthesize(text, voice: VoiceRef) -> AudioClip` |
| `SourceSeparator` | `separate(audio_key) -> {vocals_key, bgm_key}` |
| LLM | Model string của pydantic-ai |

Workflow chọn provider qua `VoiceRef.provider` và một registry trong worker.

### 4.6 Contract dữ liệu (Pydantic)

Các model dữ liệu dùng chung: `LocalizationInput`, `VoiceRef`, `Transcript`, `SubtitleTrack`,
`LocalizedScript`, `DubbedAudio`, `EditSpec`, `ReviewVerdict`.

`EditSpec` là ranh giới Python → Node. Nó được serialize thành JSON và validate ở cả hai phía.

## 5. Observability và eval (Langfuse tự host)

### Tracing

- pydantic-ai phát span OpenTelemetry, Langfuse nhận qua endpoint OTLP.
- Dùng `TracingInterceptor` của `temporalio.contrib.opentelemetry` để truyền trace context từ
  workflow xuống activity. Nhờ vậy mỗi workflow run là một trace.
- Gắn các thuộc tính sau lên trace:
  - `session.id = aweme_id`
  - tag `channel`
  - tag `voice.provider`
  - phiên bản prompt
- Các provider không phải LLM (STT, OCR, TTS) tự bọc span thủ công, có thuộc tính chi phí.

### Score online

- Verdict của Reviewer.
- Các metric deterministic: tỷ lệ segment tràn, stretch trung bình, số chữ Trung còn sót.

### Eval offline

- Tạo dataset trên Langfuse từ input của Localizer (transcript + OCR) của các video được chọn.
- Chạy experiment mỗi lần đổi prompt hoặc model.
- Evaluator kiểu LLM-as-judge chấm độ trung thành và độ tự nhiên, kết hợp với
  `estimate_duration` để chấm độ vừa thời lượng.

### Lưu ý

Nếu sau này quản lý prompt trên Langfuse thì phải fetch prompt trong activity, hoặc pin phiên
bản prompt vào input của workflow. Không được fetch prompt trong code workflow.

## 6. Hạ tầng (`docker-compose`)

**Service đã có:** `nest` (downloader), `parser`, `web`, `postgres`, `redis`.

**Service thêm mới:**

| Service | Ghi chú |
|---|---|
| `temporal` | Temporal server |
| `temporal-ui` | Giao diện web của Temporal |
| `temporal-admin-tools` | Chỉ dùng để setup schema |
| `clickhouse` | Langfuse v3 bắt buộc phải có. Tốn nhiều RAM nhất (2–4GB) |
| `langfuse-web` | |
| `langfuse-worker` | |
| object storage | MinIO, xem mục 7 |
| `worker-agents` | Python. Task queue `llm`, `io`, `tts` |
| `worker-media` | Python + ffmpeg + Demucs. Task queue `media` |
| `worker-render` | Node + Remotion + Chromium. Task queue `render` |

**Dùng chung:**

- **Postgres:** một instance, chia như sau.

  | Database | Schema | Chủ sở hữu |
  |---|---|---|
  | `reup` | `public` | downloader (giữ nguyên) |
  | `reup` | `localization` | workflow DB: outbox, glossary, kết quả, `needs_human` |
  | `temporal` | | Temporal |
  | `temporal_visibility` | | Temporal |
  | `langfuse` | | Langfuse |

- **Redis:** một instance dùng chung cho cookie store và hàng đợi của Langfuse.
  - Langfuse yêu cầu `maxmemory-policy noeviction`. Đây là giá trị mặc định của Redis, nhưng nên
    pin tường minh trong compose để không bị đổi nhầm.
  - Langfuse dùng database index riêng.
- **Object storage:** một instance, tách theo bucket.

  | Bucket | Nội dung |
  |---|---|
  | `reup-media` | `source/`, `work/{aweme_id}/` (lifecycle tự xoá sau 7 ngày), `output/` |
  | `langfuse` | events và media của Langfuse |

  Mỗi bucket có access key riêng.

## 7. Quyết định đã chốt

### 7.1 Postgres: một instance, mỗi hệ một database

Temporal và Langfuse dùng database riêng thay vì schema riêng (người dùng đã đồng ý). Lý do:
- **Temporal:** server không có config chính thức cho custom schema. Trên forum có báo cáo server
  không tìm thấy bảng `schema_version` khi chạy với schema riêng.
- **Langfuse:** tự chạy migration bằng Prisma lúc khởi động.

Workflow DB vẫn nằm ở schema `localization` trong database `reup`.

### 7.2 Object storage: interface S3 API, provider chỉ là cấu hình

- Code chỉ dùng S3 API (Python dùng `boto3`/`aioboto3`; downloader TS dùng `@aws-sdk/client-s3`).
- Local dùng MinIO. Production chọn S3 hoặc R2 sau.
- Đổi provider chỉ là đổi `endpoint`, `region`, credentials, `forcePathStyle` trong config,
  không đụng tới code.
- Bối cảnh: MinIO community đã bị archive (năm 2026), nên MinIO chỉ dùng cho môi trường local.
- Downloader cần thêm một implementation S3 cho interface `Storage` hiện có, với điều kiện giữ
  nguyên tính atomic: object chỉ xuất hiện dưới key cuối khi đã ghi xong, và hook `beforeCommit`
  phải chạy trước bước commit.
- Không dùng tính năng riêng của từng provider (ví dụ lifecycle rule) trong code. Lifecycle là cấu
  hình hạ tầng, không phải hành vi mà ứng dụng dựa vào.

### 7.3 LLM: OpenAI-compatible, mặc định qua OpenRouter

- Dùng `OpenAIChatModel` của pydantic-ai, với `base_url` và model id lấy từ config.
- Từng vai có thể dùng một model khác nhau (Localizer, Dubber, Reviewer, OCR).
- Model được chọn phải hỗ trợ tool calling. Reviewer và OCR cần model đọc được ảnh.

### 7.4 OCR: vision LLM qua OpenRouter, provider OCR chuyên dụng là phương án dự phòng

`OcrProvider` có hai implementation:
- **Mặc định:** vision LLM qua OpenRouter, trả về chữ kèm bbox. Không thêm vendor mới.
- **Dự phòng:** OCR chuyên dụng, dùng khi bbox của vision LLM không đủ chính xác.

### 7.5 STT: theo chuẩn OpenAI `/v1/audio/transcriptions`, VoiceStudio là ứng viên đầu tiên

OpenRouter là API chat, không có STT kèm timestamp. Vì vậy STT cần một provider riêng.

**Implementation mặc định của `SttProvider`.** Dùng `OpenAICompatSttProvider`, gọi
`POST /v1/audio/transcriptions` với:
- `response_format=verbose_json`
- `timestamp_granularities[]=word,segment`

Cùng một implementation này gọi được VoiceStudio, Groq, OpenAI `whisper-1`, hoặc bất kỳ server
tương thích nào khác. Lưu ý: `gpt-4o-transcribe` của OpenAI không trả timestamp mức từ.

**VoiceStudio đã có endpoint này** (đọc code ở commit `6d0227c`, file
`backend/api/routers/openai_compat.py`):
- Timestamp mức từ chỉ có khi `response_format=verbose_json`.
- Request luôn chạy trên ASR engine đang được active ở server. Không chọn engine theo từng
  request được, nên engine là cấu hình của server.
- Các engine có sẵn:
  - `whisperx`: Whisper large-v3 kết hợp forced alignment wav2vec2. Đây là engine mặc định.
  - `faster-whisper`
  - `funasr`: SenseVoice, mạnh về tiếng Trung, có diarization cam++.
  - `openai-compat-asr`: chuyển tiếp tới một server ASR khác, ví dụ Qwen3-ASR.

**Cách triển khai đã chốt.** VoiceStudio chạy trên một máy khác do người dùng tự quản lý. Hệ
thống này chỉ biết tới nó qua URL, và không đưa nó vào `docker-compose`.

Cấu hình qua env của worker:
- `VOICESTUDIO_URL`
- `VOICESTUDIO_API_KEY` (bearer token)

Hệ quả cho thiết kế:
- Worker tải audio từ object storage rồi upload multipart lên VoiceStudio, vì máy VoiceStudio
  không đọc được MinIO.
- Đường truyền phải đi qua TLS hoặc mạng riêng (Tailscale/VPN), vì có cả audio lẫn API key.
- VoiceStudio thường chỉ có một GPU, nên task queue `stt` và `tts` cần giới hạn concurrency
  thấp (bắt đầu từ 1–2).
- Timeout và retry phải phân biệt hai loại lỗi:
  - Lỗi tạm thời: GPU đang bận, 504, mất mạng. Retry.
  - Lỗi vĩnh viễn: 400, file không có audio. Không retry.
- Có một health check tới `/.well-known/voicestudio-speech` trước khi worker nhận task.

**Ưu điểm:**
- STT và TTS cùng một server, và đây cũng chính là voice provider bạn đã định dùng.
- Không mất phí theo phút audio.

**Không dùng pipeline `/dub/*` của VoiceStudio.** State của job nằm trong DB riêng của
VoiceStudio, nên không durable trên Temporal và không trace được trên Langfuse. Chỉ dùng
VoiceStudio làm provider cho các thao tác đơn lẻ (STT, TTS).

**Spike để chọn STT:** chạy 3–5 video Douyin thật qua các ứng viên sau, rồi so độ chính xác của
chữ Trung và độ khớp timestamp:
- VoiceStudio với engine `whisperx`
- VoiceStudio với engine `funasr`
- Một API cloud tương thích OpenAI

## 8. Trade-offs

- **Workflow cố định + bước agentic** (đã chọn).
  - Dựa vào giả định: các bước xử lý giống nhau giữa các video.
  - Hỏng đầu tiên khi: các video cần chuỗi xử lý khác hẳn nhau.
- **Agent điều phối toàn bộ.**
  - Dựa vào giả định: LLM ra quyết định ổn định.
  - Hỏng đầu tiên ở: chi phí, độ bất định, và retry khó kiểm soát.
- **Dubber làm agent toàn phần.**
  - Cách này cho agent tự TTS toàn bộ segment.
  - Hỏng ở: mỗi segment tốn một lượt model, nên 100 segment là 100 lượt.
  - Vì vậy đã thu hẹp scope: chỉ xử lý các segment bị tràn.

## 9. Rủi ro còn lại

- Tích hợp Temporal của Pydantic AI vẫn là beta.
- Vòng lặp fit-timing cho tiếng Việt là phần khó nhất về chất lượng.
- Che sub bằng hộp mờ phụ thuộc vào việc vị trí sub có cố định hay không.
- Demucs chạy CPU sẽ chậm khi backfill.
- ClickHouse tốn RAM.
- Trên Langfuse, span của các tool chạy thành activity có thể hiển thị lệch cha-con nếu
  interceptor không được cấu hình ở cả workflow lẫn activity.
