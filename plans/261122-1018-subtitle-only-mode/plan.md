---
status: done
branch: feat/localization-dubber-code-mode
---

# Chế độ "chỉ dịch sub" (subtitle-only)

## Outcome

Một video tải qua extension với tuỳ chọn "Chỉ dịch sub" chạy luồng rút gọn:
tìm sub cháy → đọc chữ sub → dịch → chèn sub Việt lên box → dựng review project sẵn sàng
render. Không Demucs, STT, voice_rate, phân loại quảng cáo, detect_singing, TTS, Dubber, mix
nhạc nền. Audio của video ra là audio gốc.

## Quyết định đã chốt (người dùng, 2026-11-22)

- Đánh dấu theo **từng video**, bằng toggle trong extension. Không thêm cài đặt theo collection.
- Không tìm thấy sub cháy hoặc OCR không đọc được dòng nào: dừng, publish `needs_human` kèm lý do
  "không có sub để dịch". Không fallback sang STT.
- Box che sub và làm mờ watermark giữ như luồng đầy đủ. Box hiện **xuyên suốt video**, theo
  quy tắc hiện tại của review project (README đã sửa lại cho đúng).

## Non-goals

- Không đổi luồng đầy đủ, luồng Vbee hay `legacy_render`.
- Không hỗ trợ revoice hay remix cho project chỉ sub (Studio vẫn sửa được chữ và thời gian).
- Không thêm chế độ theo collection.

## Thiết kế

### Đánh dấu và mang chế độ đi

1. Extension: thêm tuỳ chọn "Chỉ dịch sub" khi enqueue; gửi `POST /api/videos {url, localize, mode}`
   với `mode ∈ {"full","subtitles_only"}` (bỏ trống = `full`).
2. Downloader: migration thêm `videos.localize_mode text NOT NULL DEFAULT 'full'
   CHECK (localize_mode IN ('full','subtitles_only'))`. Schema zod, service và repository ghi giá trị
   này khi intake. Video đã có trong DB mà enqueue lại: ghi đè mode.
3. Starter: `VIDEO_SQL` đọc `v.localize_mode`, đưa vào `LocalizationInput.localize_mode`
   (mặc định `"full"` để input cũ vẫn hợp lệ). Chế độ chỉ sub chỉ chạy với `delivery_mode=review_project`
   (provider review). Nếu không thoả, reject row và ghi lý do rõ ràng.
4. Dashboard: hiển thị nhãn "Chỉ sub" trên video và pipeline (chỉ đọc).

### Workflow (`VideoLocalizationWorkflow`, có `workflow.patched("subtitles-only")` bảo vệ)

```
check_voice (bỏ qua nhánh "no_voice → published_original"; vẫn skip file không có video)
capture_review_source
detect_subtitles → detect_watermarks
  region None → publish needs_human ("không có sub để dịch")
complete_transcript(transcript rỗng)        # mọi cue thành segment source="subtitle"
  0 segment → publish needs_human
Localizer (contextual, không cần voice rate cho TTS)
keep_source_audio                          # activity mới, xem dưới
layout_captions → review_before_render (chỉ phần caption/layout)
build_review_project → publish ready_for_review | needs_human
```

- `complete_transcript` vốn đã đọc chữ sub cháy ở các đoạn không có lời nói. Cho nó một transcript
  rỗng là đọc toàn bộ sub. Cần một transcript rỗng hợp lệ trong store, tạo bởi một activity nhỏ
  hoặc cho `complete_transcript` nhận `None`.
- `keep_source_audio` (activity mới, queue `media`): ffmpeg tách audio gốc của source snapshot
  thành wav và ghi `DubSegmentsDocument` với mọi segment `keep_original=True` (không clip, đúng
  validator hiện có). Activity trả `DubbedAudio`, nên layout, review và build project không đổi chữ ký.
  `IngestResult` được dựng từ probe (`duration/width/height/fps`), với `bgm_key` là audio gốc và
  không có vocals.
- `build_review_project`: chấp nhận `vocals=None` (đã chấp nhận), `bgm = mixed`. Manifest thêm
  `localize_mode` để Studio/review-bridge chặn revoice và remix với project chỉ sub.

### Render cuối

`review_worker` render từ `mixed_audio` của manifest, tức audio gốc. Phải kiểm tra lại rằng nó
không remix từ stem khi project chỉ sub.

## Phases

| # | Phase | Files chính |
|---|---|---|
| 1 | Contract: `localize_mode` trên `LocalizationInput` và manifest; transcript rỗng | `contracts/models.py`, `contracts/review_project.py` |
| 2 | Activity `keep_source_audio` + test | `activities/` mới, đăng ký worker media |
| 3 | Nhánh workflow + patch + test replay | `workflows/video_localization.py`, tests/workflows |
| 4 | Starter đọc mode, reject tổ hợp sai | `starter/outbox_starter.py`, tests |
| 5 | Downloader: migration, schema, intake | `services/downloader/migrations/`, `videos/*` |
| 6 | Extension toggle + test | `services/extension/*` |
| 7 | Chặn revoice/remix theo manifest; dashboard nhãn | `review_bridge/`, `services/web` |
| 8 | Chạy thật một video chỉ sub end-to-end, xuất vào `data/review` | — |

## Acceptance criteria

- Video enqueue với "Chỉ dịch sub" ra review project có sub Việt trên box, audio gốc nguyên vẹn, và
  trace không có ingest/transcribe/TTS/singing/classify.
- Video không có sub cháy kết thúc `needs_human` với lý do rõ ràng.
- Video enqueue không có tuỳ chọn chạy y hệt trước; các run đang bay replay được (test replay).
- Revoice và remix bị từ chối trên project chỉ sub, có thông báo.
- Mutation test cho nhánh mới chạy một lần cuối plan (theo quy ước dự án).

## Rủi ro

- Localizer theo ngữ cảnh có thể giả định có transcript speech hoặc voice rate. Kiểm tra ở phase 3.
- OCR đọc chữ toàn bộ video tốn hơn chỉ đọc khoảng trống. Đo chi phí trên video thật ở phase 8.

## Chạy thật (2026-11-22)

Video `7701648680104758554` (54 giây, sub cháy tiếng Trung), bấm nút Sub trên extension.

| Lượt | Thay đổi | Số câu | Kết quả |
|---|---|---|---|
| g1 | luồng chỉ sub như thiết kế | 7 | thiếu dòng nằm ngoài cue thưa; mọi câu `inferred` |
| g2 | đọc toàn dải sub, 2 frame/giây | 13 | đủ dòng, nhưng hai câu giống 0.6 bị gộp |
| g3 | ngưỡng nối dòng 0.8 | 14 | đủ dòng; một câu dịch thành câu trước |
| g4 | dòng bắt đầu ở frame đầu tiên đọc được | 14 | đúng cả 14 câu, `ready_for_review`, $0.0074 |

Quyết định của người dùng: đọc OCR 2 frame/giây thay vì STT; câu đọc từ màn hình là nguồn
chính thức của run chỉ sub, không đánh `inferred`.

