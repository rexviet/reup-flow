# Phase 09: score, span provider và eval chạy được trên Langfuse thật

**Date**: 2026-10-28 21:15 (Asia/Saigon)
**Severity**: Medium
**Component**: services/localization — observability, eval
**Status**: Ongoing (phần cốt lõi đã xong, còn nợ eval)

## Việc đã làm

Phase 09 (`plans/261026-0147-video-localization-workflow/phase-09-observability-and-eval.md`) đã được triển khai. Activity `publish` gửi Langfuse score lên trace của run. Các span `provider.stt`, `provider.tts`, `provider.ocr` chỉ mang `provider`, `model` và `input_units`, không mang nội dung. Có test kiểm tra trace đầy đủ, kèm một bản đối chứng không có interceptor để chứng minh test thật sự phân biệt được. Các script eval gồm `build_dataset`, `run_experiment`, `evaluators`, `ocr_eval`, `pipeline_eval`. Box OCR thô theo từng frame giờ được lưu vào `subtitles.json`.

AC8 đã được xác minh qua Langfuse API. Có hai dataset run trên `localizer-v1` (5 video thật), và item nào cũng có đủ `fit_ratio`, `fidelity`, `naturalness`. Bản v4-pro đạt fidelity 0.90, naturalness 0.80, tốn $0.036. Bản v4.1-flash đạt 0.90 và 0.75, tốn $0.022.

## Sự thật khó nghe

Langfuse thật trả HTTP 400 khi một score mang cả `traceId` lẫn `sessionId`. Các test chạy trên stub server đều xanh và không hề bắt được lỗi này, vì stub chấp nhận mọi thứ. Nếu không chạy trên dịch vụ thật thì score sẽ rơi mất trong im lặng ở production. Đây lại là một lần nữa test double không thể hỏng giống thực tế.

## Sửa lại nhận định cũ

Đã tìm ra nguyên nhân gốc của E6. Ở hai video dài bị lỗi, toàn bộ transcript nằm trong một segment duy nhất, dài 2.220 và 2.996 byte base64, trong khi giới hạn là 1.400.

Nhận định E2 trước đây là sai. Bốn video có transcript rỗng thực ra có stem vocals gần như im lặng (RMS 8–51, so với bgm 4.000–13.000). Vì vậy không có bằng chứng nào cho thấy giọng gốc bị mất trong pipeline.

## Review độc lập

Review không tìm thấy blocker. Các điểm đã sửa: bỏ qua score khi span của activity là root (nếu không sẽ tạo trace lạc hoặc score trùng khi retry); bọc phần chấm điểm để nó không bao giờ làm `publish` thất bại; sửa một test rỗng (vacuous). Lưu ý vận hành: nếu rolling deploy để worker cũ chạy cùng worker mới thì worker cũ sẽ từ chối các field contract mới, nên phải restart cả bốn worker cùng lúc.

## Bài học

Một tích hợp với dịch vụ ngoài chỉ được coi là xong khi đã chạy trên dịch vụ thật. Stub chỉ chứng minh code gọi đúng hình dạng mà ta tưởng, không chứng minh dịch vụ chấp nhận nó.

## Việc tiếp theo

Bộ test đầy đủ: 1288 passed. Chưa làm: OCR eval trên frame do người gán nhãn, E4 với lỗi gieo sẵn, E5 hiệu chỉnh ngưỡng Jev, AC7 trên một run thật, và các mutation run (hoãn đến sau phase 10).

**Status**: DONE_WITH_CONCERNS — phase 09 đạt AC8 trên Langfuse thật, nhưng OCR eval có nhãn, E4, E5, AC7 và mutation run vẫn còn mở.
