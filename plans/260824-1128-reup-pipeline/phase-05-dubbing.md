---
phase: 5
title: Module 2 — Dubbing đầy đủ + Review UI (M3)
status: not_started
effort: 5-6 ngày
blockedBy: [4]
---

# Phase 5 — Module 2: Dubbing

mp4 gốc → mp4 tiếng Việt + caption tiếng Việt, kết thúc bằng **review gate**.

Kết thúc phase này: **1.000 video qua pipeline, có bản đã duyệt kèm caption.**

> **Tiền đề:** Gate B của Phase 4 đã chốt D2-A1 (3 lane) hay D2-A2 (5 lane). Plan dưới đây viết cho **A1**. Nếu Gate B chọn A2, xem §5.7.

---

## 5.1 Ba lane

| Lane | stage | Concurrency | Việc |
|---|---|---|---|
| `dub.lane.ts` | `pending_dub` | **1** | `POST /batch/enqueue` → poll → download về `/data/dub/` |
| `caption.lane.ts` | `pending_caption` | 4 | `POST /dub/translate` một segment → `renditions.caption` |
| `render.lane.ts` | `pending_render` | 2 | ffprobe + conform 9:16 / 1080×1920 / AAC 48kHz qua `execa` |

**`prepare.lane` không tồn tại.** Probe gộp vào `render` (D3 xoá gate 3–90s nên không cần lane chặn sớm); `width/height/fps` cho curation lấy từ `aweme_list[]` ở Module 1.

### `dub` và `caption` độc lập, **cùng chặn review**

```
pending_dub ──┐
              ├──> pending_render ──> pending_review
pending_caption ──┘
```

Caption đi qua LLM (nhanh, rẻ). Dub đi qua GPU (chậm). Rendition vào `pending_review` **chỉ khi cả hai xong**.

> Quên điều kiện này thì reviewer sẽ thấy video không có caption — và sẽ approve nó, vì video thì ổn. Đây là bug lọt ra tới page thương hiệu.

Cách làm: `render.lane` chuyển sang `pending_review` chỉ khi `caption IS NOT NULL`; nếu chưa có thì để lại `ready` với `next_attempt_at` ngắn.

---

## 5.2 `omnivoice.client.ts` — bốn điều bắt buộc

Đây là file nhiều bug tiềm ẩn nhất của cả hệ thống, vì bên kia là một service **không ổn định có chủ ý** (Colab).

### 1. Base URL đọc Redis **mỗi request**

```ts
const base = await redis.get('omnivoice:base_url')   // MỖI request, không cache
```

Colab: session chết ≤12h, có thể bị preempt, **URL tunnel đổi mỗi lần restart**. **Không hardcode `localhost:3900`.**

`POST /internal/omnivoice` (có auth) để notebook Colab tự đăng ký URL mới — đối xứng với `POST /internal/cookie`.

> Đây cũng là lý do **không dùng SDK `openai`** dù OmniVoice OpenAI-compatible: SDK giữ base URL trong client instance, nên phải tạo client mới mỗi lần gọi — chống lại thiết kế của nó. Chỉ có 2 endpoint (multipart in, binary out) nên `fetch` thẳng vừa ngắn hơn vừa đúng hơn.

### 2. Health gate **trước khi claim**

Fail ⇒ nhả lease, **không tăng `attempts`**.

> Không có phần này thì mỗi lần Colab restart đẩy **hàng trăm rendition sang `failed`** — và không có đường về ngoài SQL tay.

### 3. Poll trả **404 ⇒ enqueue lại**, không phải `failed`

Job state của OmniVoice là in-memory: `_queue = asyncio.Queue()`, `_jobs = {}`. Restart là mất hết. `renditions.stage` là chân lý duy nhất — 404 nghĩa là "bên kia quên", không phải "job này lỗi".

### 4. **409 = thiếu model ASR ⇒ `SetupError`**

Typed body `asr_model_missing_error`. Đây là lỗi **setup**, không transient. Nhả lease, **không tăng `attempts`**, đẩy lên health badge. Cùng cách xử lý như health check fail.

> Retry một `SetupError` là retry vô hạn. Đánh dấu `failed` là mất rendition vì một lỗi cấu hình sửa 30 giây.

### Lease renewal trong lúc poll

Job dub chạy vài phút. `dub.lane` phải renew lease trong vòng poll, không thì reaper giật item khỏi lane đang xử lý.

### `GPU_SEMAPHORE_SIZE=1`

Khớp với `_worker()` của OmniVoice — nó xử lý **một job một lúc**. Không phải chọn, mà là khớp thực tế bên kia.

---

## 5.3 `caption.lane.ts` + `policy/caption.ts`

```
videos.description  ← desc thô, nguyên văn (Module 1 lưu không xử lý)
        │
        │  policy/caption.ts — HÀM THUẦN
        ├─ strip @mention        (vô nghĩa trên Facebook)
        ├─ tách hashtag ra khỏi prose
        └─ giữ emoji
        │
        ▼  POST /dub/translate — MỘT segment
        │    provider=openai · quality=cinematic · glossary=talent_glossary
renditions.caption  ← prose tiếng Việt
        │  + fanpages.default_hashtags   (hashtag của page, KHÔNG dịch từ tiếng Trung)
        ▼
    Reviewer SỬA ĐƯỢC
        ▼
publications.caption → tham số `description` của POST /videos
```

### Ba quyết định trong sơ đồ

1. **`desc` thô lưu ở Module 1, xử lý ở Module 2.** Đổi luật làm sạch không được phải tải lại từ Douyin.
2. **Không dịch hashtag tiếng Trung.** `#萌宠` dịch ra không phải hashtag người Việt tìm. Thay bằng `fanpages.default_hashtags` — người chọn cho từng page.
3. **Caption sửa được trong review gate.** Nó là text công khai lên page thương hiệu. Reviewer đang xem video rồi thì đọc caption là chi phí ~0; để nó lọt ra ngoài không ai xem là rủi ro thật.

### `@anthropic-ai/sdk` không có trong dependency

Caption dịch qua `POST /dub/translate` của OmniVoice. **API key dịch nằm trong env của OmniVoice, không phải của ta** — bớt một secret phải quản.

Và `glossary` (tên talent, tên sản phẩm) áp được **ngay cho caption** dù đường batch của thoại chưa nhận nó.

---

## 5.4 `render.lane.ts` + `policy/spec-conform.ts`

- ffprobe đọc thông số thật (không tin `aweme_list[]` cho bước này — file đã qua dub).
- Conform **9:16 / 1080×1920 / AAC 48kHz**.
- Spawn ffmpeg bằng **`execa`**, không `fluent-ffmpeg` (không còn được bảo trì; spawn trực tiếp cho quyền kiểm soát filter graph, thứ mà stage này cần).
- Đọc/ghi qua `storage.pathFor()` — **ffmpeg nhận đường dẫn trực tiếp**, không tạm file. Đây là toàn bộ lý do chọn ổ đĩa ở D5.

**Vẫn render 9:16 dù `/videos` không enforce.** Aspect ratio ảnh hưởng **hiệu quả** ngay cả khi không ảnh hưởng tính hợp lệ — video 16:9 trong feed Reels hiển thị kém.

`needs_conform` cho biết có phải scale/pad hay không; nếu video gốc đã 9:16 thì copy stream, không re-encode.

---

## 5.5 `policy/curation.ts`

Rank theo engagement từ `stat_*` (đã có sẵn từ Module 1, **không thêm lời gọi API nào**). Hàm thuần, test không cần DB.

Curation là **cửa vào** Module 2: `videos.stage = downloaded` → tạo `renditions` hàng mới ở `pending_dub`.

**`voice_consent_at` phải nằm trong câu claim của `dub.lane`**, không ở tầng service:

```ts
.where(and(
  eq(renditions.stage, 'pending_dub'),
  eq(renditions.status, 'ready'),
  isNotNull(talents.voiceConsentAt),        // ← ĐIỀU KIỆN TRONG CÂU CLAIM
  ...
))
```

> Clone giọng một người cần đồng ý bằng văn bản. Đặt vào SQL nghĩa là **một đường code mới không thể đi vòng qua nó**. Ở tầng service thì một endpoint mới quên gọi check là đủ để vi phạm.

---

## 5.6 Review UI (`apps/web/`) — mặt tiền sản phẩm

`ReviewQueue` là component quan trọng nhất của cả frontend, vì nó là nút cổ chai thật khi mở rộng (10–17 giờ người/ngày ở 100 page).

| Thành phần | Chi tiết |
|---|---|
| Player | Video đã render, phát ngay không chờ tải hết |
| Waveform | **Gốc vs dub cạnh nhau** (`wavesurfer.js`) — nghe chỗ nào lệch mà không phải xem hết |
| Caption | **Sửa được** ngay trong queue |
| Approve | **Một phím.** 2 phút/rendition × 1.000 = 33 giờ; mỗi click thừa là 1.000 click |
| Reject | → **enum lý do**, không phải text tự do |

### `reject_reason` quyết định tua về stage nào

| Lý do | Tua về | Chi phí |
|---|---|---|
| `bad_caption` | `pending_caption` | **Rẻ** — chỉ LLM |
| `bad_translation`, `bad_voice`, `bad_timing`, `bad_audio_mix` | `pending_dub` | Đắt — tốn lại GPU |
| `content_unsuitable` | `rejected` (chấm dứt) | Không dub lại |

> Đây là lý do `reject_reason` phải là enum, không phải text. Text tự do làm mất cả routing **và** khả năng đo.

**`review_round > 2`** → vào hàng đợi cần người can thiệp, **không dub lại vô hạn**.

### Serve video cho player

`FileResponse` sau một signed token (~30 dòng) — `storage.signedUrl()`. Đây là lợi ích thứ 4 của MinIO mà D5 nói "tự viết ~30 dòng".

### `PipelineBoard`

`GROUP BY stage, status` — vừa là dashboard vừa là query mà UI cần. Đây là lợi ích cụ thể của D1-A1: monitoring là một câu SQL, không phải một hệ thống.

### Gotcha SSE

`EventSource` **không gửi được custom header** ⇒ không đặt `X-Api-Key` được, và **không dùng query param** (rơi vào access log). Dùng `@microsoft/fetch-event-source`.

Phía server: NestJS `@Sse()` trả `Observable<MessageEvent>`.

---

## 5.7 Nếu Gate B chọn D2-A2 (đường granular)

`dub.lane` tách thành 4 lane: `upload` → `transcribe` → `translate` → `generate` → `export`.

Hai knob mở ra — **đó là toàn bộ lý do trả giá 4 lane**:
- `voice_match: "consistent"` — MỘT reference cho mỗi speaker trong cả video, hết drift
- `timing_strategy: "smart_fit"` — chia gánh nặng giữa tăng tốc audio ≤1.2× và làm chậm video ≤2.0×, **thay vì cắt chữ**

Cộng thêm: `quality: "cinematic"`, `auto_glossary`, `glossary`, `dialect` cho thoại; sửa transcript/bản dịch giữa chừng (`/dub/import-srt`, `regen_only` per-segment).

**Chi phí thật:** phải tự giữ mảng segments giữa `transcribe → translate → generate`. Đó là state ở giữa hai lane — lưu vào một cột JSONB trên `renditions`, không giữ trong memory.

---

## 5.8 Đo từ ngày đầu — không phải nice-to-have

| Metric | Vì sao |
|---|---|
| `reject_rate` tổng | Số vào §10 sizing |
| **Phân bố `reject_reason`** | `bad_voice` / `bad_timing` chiếm phần lớn ⇒ **leo lên D2-A2** |
| Thời gian duyệt thật / rendition | Chặn kế hoạch mở rộng (10–17 giờ/ngày ở 100 page) |
| `gpu_seconds` / rendition | Số thật thay ước lượng 8–17 giờ |

> Ba đường xử lý review khi tới nút cổ chai (§10): duyệt nhanh hơn (nghe 10s đầu, ~30s/video) · duyệt mẫu rồi auto theo talent · thêm người. **Đừng thiết kế trước cho chúng** — nhưng phải có số khi cần quyết.

---

## 5.9 Tests

**Thuần:** `caption-clean` (strip mention, tách hashtag, giữ emoji) · `curation-rank` · `spec-conform`
**Hạ tầng:** `omnivoice-down-no-attempt-burn` · `omnivoice-404-requeues-not-fails` · `omnivoice-409-is-setup-error` · `consent-gate` (rendition của talent không có consent **không bị claim**) · `reject-rewinds-by-reason` · `review-blocked-until-caption-and-dub-both-done` · `lease-renewed-during-long-poll`

---

## Definition of done

- [ ] 1.000 video qua pipeline, có bản `approved` kèm caption tiếng Việt
- [ ] Restart OmniVoice giữa lúc chạy → **không rendition nào sang `failed`**, tất cả enqueue lại
- [ ] Talent không có `voice_consent_at` → rendition **không bị claim** (test, không phải review code)
- [ ] Reject `bad_caption` → tua về `pending_caption`, **không tốn GPU**
- [ ] Video vào `pending_review` **luôn** có caption
- [ ] `PipelineBoard` hiện đúng `GROUP BY stage, status`
- [ ] Bốn metric ở §5.8 xem được
