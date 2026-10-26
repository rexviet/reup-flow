---
phase: 10
title: "End-to-end, backfill thật và docs"
status: pending
priority: P1
effort: "1d"
dependencies: [9]
---

# Phase 10: End-to-end, backfill thật và docs

<!-- Updated: Red Team 2026-10-26 - runner đã refactor ở phase 03; AC1/2/3/6/7 kiểm theo định nghĩa mới; thêm kiểm tuổi workflow so với lifecycle -->

## Goal

Hai mục tiêu:
- Chứng minh AC1–AC8 trên stack thật, với video thật.
- Viết tài liệu đủ để người khác vận hành.

## Files to Create / Modify

- Modify: CI workflow đang chạy `scripts/run-mutations.sh`. Thêm bước cài `uv`, Python và deps của renderer.
- Create: `docs/localization-architecture.md`. Gồm kiến trúc, link diagram, sáu nguyên tắc, bảng
  queue và concurrency, bảng env (tham chiếu plan.md §6).
- Create: `docs/localization-runbook.md`. Gồm:
  - Bật stack.
  - Chạy lại một video (tăng `generation`).
  - Xử lý `needs_human` (`review/`).
  - Publish tay.
  - Đổi provider.
  - Theo dõi `cache/tts/`.
  - Drain trước khi deploy.
  - Dòng outbox ở trạng thái `dead` và `rejected`.
- Create: `plans/261026-0147-video-localization-workflow/reports/acceptance.md`.

## Tasks & Steps

1. **Backfill.** **Backup DB trước**, rồi chạy backfill 20 video.
2. **Bảng acceptance.**

   | AC | Cách kiểm |
   |---|---|
   | AC1 | `docker kill worker-agents` giữa bước 4, rồi start lại. Đối chiếu log request TTS của VoiceStudio theo hash (dùng metadata), đúng định nghĩa mới |
   | AC2 | Với 3 video có vòng review `dub`, so text sub trong spec với metadata `text_vi` |
   | AC3 | Đọc counter `render_invocations{spec_sha}`, gồm cả run có vòng review. Mọi giá trị phải bằng 1 |
   | AC4 | Chèn trùng outbox. Start trùng. Chạy lại một video `needs_human` bằng cách tăng generation |
   | AC5 | Đổi `VOICE_ID` giữa lúc backfill, rồi đọc input của hai workflow |
   | AC6 | Log enter/exit của `stt`, `tts` (gồm cả Dubber), `media`, `render` |
   | AC7 | Gọi Langfuse API cho 3 run. Mỗi run phải có span `render` từ Node và score |
   | AC8 | Kết quả của phase 09 |
   | Lifecycle | Tuổi của workflow đang chạy lâu nhất phải < 14 ngày, nhỏ hơn lifecycle 30 ngày |

3. **Mutation.** `bash scripts/run-mutations.sh` chạy đủ downloader, localization và renderer, đúng số lượng đã khai, cây code không đổi.
4. **Docs.** Chạy thử từng lệnh trong runbook trước khi ghi vào.

## Verification

- `acceptance.md`: mọi AC đều PASS kèm bằng chứng. AC nào fail thì quay về phase sở hữu nó.
- `bash scripts/run-mutations.sh`: exit 0.
- Mọi link trong docs mở được.

## Risk

VoiceStudio là nút cổ chai khi backfill. Ghi thông lượng đo được vào runbook.

## Rollback

Phase này chỉ thêm docs, CI và script.
