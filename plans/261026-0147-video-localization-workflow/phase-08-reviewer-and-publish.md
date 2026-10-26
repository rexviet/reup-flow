---
phase: 8
title: "Reviewer, vòng chạy lại, publish và nối end-to-end"
status: pending
priority: P1
effort: "1.5d"
dependencies: [6, 7]
---

# Phase 08: Reviewer, vòng chạy lại, publish

<!-- Updated: Red Team 2026-10-26 - Reviewer chạy trong một activity, cấm presigned URL, định tuyến dub chạy lại 4b, artifact theo vòng, review/ cho needs_human, AC2 so metadata TTS -->

## Goal

Phase này làm bốn việc:
- Nối nhánh âm thanh (phase 06) và nhánh hình ảnh (phase 07) thành pipeline đủ 9 bước.
- Bước 8 chấm điểm mp4 **bên trong một activity**.
- Chỉ chạy lại stage bị lỗi, và sub luôn khớp với lời đọc.
- Kết thúc bằng một trong hai: publish ra `output/`, hoặc `needs_human` với bản render lưu ở `review/`.

## Files to Create / Modify

- `src/localization/agents/reviewer.py`: agent pydantic-ai **thường**, không dùng `TemporalAgent`.
- `src/localization/activities/review_render.py`: activity chạy Reviewer, tool là các hàm Python bên trong activity đó.
- `src/localization/activities/publish.py`
- `src/localization/workflows/video_localization.py`
- `tests/workflows/test_review_loop.py`
- `tests/agents/test_reviewer.py`
- `tests/mutations/phase-08.sh` và dòng `EXPECT`.

## Tasks & Steps

1. **`review_render` (D11).**
   - Chạy trên queue `llm`, timeout 10 phút, tối đa 2 lần thử.
   - Bên trong activity, Reviewer có 4 tool, đều là hàm Python cục bộ:

     | Tool | Mô tả |
     |---|---|
     | `alignment_report` | Đọc `dub-segments.json`, đo LUFS và true peak trên mp4 |
     | `sample_frames` | `ffmpeg` cắt frame, trả `BinaryContent`. Tối đa 8 frame mỗi lần, 3 lần mỗi review |
     | `residual_source_text` | Chạy `OcrProvider` trên frame, tìm cue chữ Hán nằm ngoài hoặc xuyên qua hộp che |
     | `read_script` | Đọc script |

   - Ảnh chỉ nằm trong bộ nhớ của activity, **không bao giờ đi vào payload Temporal**.
   - **Cấm presigned URL.**
   - Instrument với `include_binary_content=False`.
   - Activity chỉ trả `ReviewVerdict`, payload nhỏ.
   - Retry activity thì chạy lại toàn bộ review. Đánh đổi này chấp nhận được vì review ngắn.
   - Nội dung video được bọc trong khối `<untrusted_video_text>` giống phase 05.
2. **Định tuyến chạy lại.** Code thuần, mỗi vòng `r` dùng key `work/{id}/r{r}/`.

   | Stage lỗi | Chạy lại |
   |---|---|
   | `localize` | 3 → 4 → 5 → (6 ‖ 4b) → 7. Truyền `issues` cho Localizer làm feedback |
   | `dub` | 5 (chỉ các segment được nêu) → script mới → **(6 ‖ 4b)** → 7 |
   | `caption` | 4b → 7 |

   Spec mới mang `script_version` và `audio_sha256` mới, nên `spec_sha` đổi và render chạy thật.
   Spec không đổi thì render được bỏ qua đúng.

   Giới hạn `review_rounds ≤ 2`. Hết lượt thì `needs_human`. Cờ an toàn từ phase 05 cũng dẫn tới `needs_human`.
3. **`publish`.**
   - **Đạt:** `CopyObject` sang `output/{aweme_id}.mp4`, upsert `runs`, `status='published'`.
   - **`needs_human`:** `CopyObject` sang `review/{aweme_id}-g{n}.mp4` (không có lifecycle), upsert `runs`. **Không ghi `output/`.**
   - Idempotent theo `(aweme_id, generation)`.
4. **Replay.** Lưu history của các run sau phase này vào `tests/replay/histories/phase-08/`.

## Verification

Chạy `uv run pytest tests/workflows/test_review_loop.py tests/agents/test_reviewer.py`.

| Kịch bản | Kỳ vọng |
|---|---|
| Fail `dub` (segment 3), rồi pass | Bước 5 (chỉ segment 3), 6, 4b và 7 chạy lại. Bước 2a, 2b, 3 không chạy lại. Text sub trong spec mới **bằng** metadata `text_vi` của các object TTS trong `DubbedAudio` (AC2) |
| Fail `caption`, rồi pass | Chỉ 4b và 7 chạy lại |
| Fail cả 2 vòng | `review/` có object. `HeadObject output/...` trả 404 |
| Pass ngay | `output/` có object, `runs.status='published'` |
| Review với 8 frame thật | Không có payload nào trong history vượt 256KB. Không có chuỗi base64 ảnh (`iVBOR`, `/9j/`) nào trong history |

**Mutation:** `bash scripts/run-mutations.sh localization/08` đỏ với:
- Chạy lại toàn bộ khi lỗi `dub`.
- Lỗi `dub` mà không chạy 4b.
- Publish khi đang `needs_human`.

**Thủ công:** một video thật chạy end-to-end và ra `output/{aweme_id}.mp4` (mốc M4).

## Risk

Vision model đắt. Đã giới hạn số frame và số lần gọi tool.

## Rollback

Workflow dừng ở bước 7. Publish tay bằng `mc cp`, có ghi trong runbook.
