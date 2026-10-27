---
phase: 9
title: "Score và eval trên Langfuse"
status: pending
priority: P2
effort: "1d"
dependencies: [8]
---

# Phase 09: Score và eval trên Langfuse

<!-- Updated: Red Team 2026-10-26 - thêm naturalness, bỏ glossary_consistency, kiểm AC7 qua API, span provider không chứa nội dung -->

## Goal

Hai mục tiêu:
- Mỗi run có score gắn trên trace của chính nó (AC7).
- Có quy trình eval chạy lặp lại được để so phiên bản của Localizer (AC8).

## Files to Create / Modify

- `src/localization/observability/scores.py`
- `src/localization/observability/provider_spans.py`
- `src/localization/activities/publish.py`
- `services/localization/eval/build_dataset.py`
- `services/localization/eval/run_experiment.py`
- `services/localization/eval/evaluators.py`
- `services/localization/eval/ocr_eval.py` (eval OCR phụ đề, xem bước 6)
- `tests/observability/test_trace_completeness.py`

## Tasks & Steps

1. **Liên kết trace.** Activity `publish` lấy trace id từ OTel context, rồi ghi score qua Langfuse public API.
2. **Score.**
   - Từ Reviewer: `fidelity`, `timing`, `visual`.
   - Deterministic: `overflow_rate`, `mean_stretch`, `residual_han_cues`, `review_rounds`.
   - Boolean: `needs_human`, `safety_flag`.
3. **Span provider.**
   - Tên span là `provider.{stt|tts|ocr}`.
   - Thuộc tính: `provider`, `model`, `input_units`.
   - Không chứa text, audio, key hay URL. Canary của phase 03 phủ cả span này.
4. **Dataset.** `build_dataset.py --videos N`: lấy transcript và subtitle của các run đã publish,
   tạo dataset `localizer-v1`.
5. **Experiment.** `run_experiment.py --model X --prompt-version Y` chạy Localizer như agent thường, ngoài Temporal.

   | Evaluator | Cách chấm |
   |---|---|
   | `fit_ratio` | Deterministic, dùng `vi_syllables` |
   | `fidelity` | LLM-as-judge, `JUDGE_MODEL`, có rubric |
   | `naturalness` | LLM-as-judge, `JUDGE_MODEL`, có rubric |
6. **Eval OCR phụ đề** (`eval/ocr_eval.py`). Chạy `VisionOcr` và bước dựng hộp che trên một tập khung có
   nhãn tay, ngoài Temporal. Lý do: toàn bộ lỗi OCR gặp ở lần chạy thật (mục "Lỗi OCR đã gặp" bên dưới) đều
   lọt qua test đơn vị, và phép đo G5 ban đầu bị nhiễu vì dùng khung nguồn có sẵn phụ đề.

   | Chỉ số | Cách chấm |
   |---|---|
   | `box_iou` | IoU giữa hộp model (sau khi giải mã đúng quy ước) và nhãn tay. Ngưỡng của G5: median ≥ 0.7, p10 ≥ 0.5 |
   | `text_coverage` | Tỷ lệ điểm ảnh chữ nằm trong hộp che cuối. Mục tiêu 100% trên khung có nhãn |
   | `cover_tightness` | Diện tích hộp che / diện tích khung chữ. Mục tiêu ≤ 2.5; hộp một dòng ở 1080×1920 cao ≤ 130 px |
   | `unusable_rate` | Tỷ lệ khung model trả văn xuôi, rỗng, hay hộp ngoài khung |
   | `false_null_rate` / `false_box_rate` | Khung có phụ đề mà model trả null; khung không phụ đề mà model trả hộp |
   | `cost_per_frame`, `cost_per_run` | Lấy từ `usage.cost` của OpenRouter, không dùng bảng giá |

   Tập nhãn: ít nhất 30 khung từ ít nhất 5 clip, có kiểu phụ đề khác nhau (trắng viền đen, có màu, giữa khung,
   nền sáng) và vài clip không có phụ đề cháy sẵn. So sánh theo từng trục: model OCR, cách viết prompt và
   quy ước toạ độ, suy luận bật hay tắt, kích thước khung gửi đi (576 hay 1024), tần số lấy mẫu.
   Nhãn do người vẽ trên khung thật, không vẽ chữ giả chồng lên khung nguồn.

## Verification

**Test tự động.** `uv run pytest tests/observability` với exporter in-memory, chỉ phủ các span Python.
- Phải đủ các span `ingest`, `provider.stt`, `provider.ocr`, model request của Localizer, `provider.tts`, `review_render`.
- Bỏ interceptor ở worker thì test đỏ.

**Kiểm AC7 đầy đủ, gồm span `render` từ worker Node:** làm ở phase 10, qua API của Langfuse.

**Thủ công.**
1. Build dataset 5 video.
2. Chạy experiment hai lần với hai model khác nhau.
3. Trên Langfuse thấy hai dataset run có đủ ba score (AC8).
4. Chụp màn hình, lưu vào `reports/`.

## Lỗi OCR đã gặp khi chạy thật (2026-10-27)

Nguồn: các lần chạy thật trên clip `7575765838708520689` (11 giây, 1080×1920, phụ đề Hán cháy sẵn ở khoảng 69% chiều cao) và `7665948504426075877`. Mô hình OCR: `deepseek/deepseek-v4.1-flash` qua OpenRouter. Đây là đầu vào cho bước 6.

| # | Lỗi | Bằng chứng | Trạng thái |
|---|---|---|---|
| 1 | `OCR_BBOX_FORMAT` đặt sai (`xyxy_px`); model trả `xyxy_norm1000` | Khung thật ở 1,0 s: chữ x 200–860, y 1296–1372 (nguồn 1080×1920) tương ứng 185–796 và 675–715 trong hệ 0–1000; model trả `[186, 677, 817, 721]`. Đọc theo pixel, trục x lệch gần 1,8 lần, 12 trên 22 khung ra hộp ngoài khung | Đã sửa cấu hình. Phép đo G5 đầu tiên sai vì khung nguồn có phụ đề thật nên model khoanh phụ đề thật, không phải dải chữ giả đã vẽ |
| 2 | Một khung có hộp ngoài khung hay sai cấu trúc làm hỏng cả workflow | `ocr: bbox is empty after clamping to the frame` ở bước `detect_subtitles` | Đã sửa: khung đó là "không phát hiện" |
| 3 | Model trả văn xuôi thay JSON | 1 trên 22 khung: "Looking at the frame, there is no burned-in subtitle text present…" (ý là không có phụ đề) | Đã sửa: bỏ khung đó và đếm; fail nếu quá nửa số khung (tối thiểu 4) không dùng được |
| 4 | Chi phí do token suy luận | Suy luận bật: 255–5.472 token đầu ra mỗi lần, trung bình $0,001235 mỗi lần; tắt (`reasoning.enabled=false`): 16 token, $0,000067 (rẻ hơn khoảng 18 lần); `effort: low` vẫn tới 7.271 token, vô ích. Run 23 giây: 55 lần gọi (46 ở `detect_subtitles`, 9 ở `review_render`); run 11 giây: 44 lần (22 + 22) | Đã sửa: tắt suy luận và đặt trần `max_tokens` |
| 5 | Cue bị cắt khi model trả hộp ngoài khung ở vài khung liền nhau | Ba khung liên tiếp (3,0–4,5 s) bị bỏ, `MAX_MISSED_FRAMES = 1` cắt cue, phụ đề lòi ra ở 3480 ms | Đã sửa: nối khoảng trống tới 2000 ms |
| 6 | Hộp che quá to | Hộp cao 219 px (g6) và vùng phụ đề cao 246 px (g8) trong khi chữ cao khoảng 80 px. Lề 30% và 3%, phóng thêm 6%, hợp của mọi khung đều cộng dồn | Đang sửa (xem dòng 7) |
| 7 | Khớp điểm ảnh làm xấu đi trên khung thật | 7 trên 12 khung ra hộp cao 78–118 px thay vì 39–61 px của model, vì vạch trắng đường dành cho người đi bộ và đèn bị coi là chữ. Hai khung ngoại lai (y 860–924 và 770–852) kéo vùng hợp lên; chia theo mười phần chiều cao còn cắt đôi một dòng chữ | Quyết định: bỏ khớp điểm ảnh, dùng đồng thuận (trung vị) trên hộp của model. Đang làm |
| 8 | Hộp che mờ 92% vẫn để lộ chữ | Chữ trắng viền đen tương phản mạnh nên vẫn đọc được xuyên qua hộp; Reviewer báo đúng | Đã sửa cấu hình: `COVER_OPACITY=1` |
| 9 | Chi phí LLM không hiện trong Langfuse | `GENERATION` có token nhưng `calculatedTotalCost = 0`. OpenRouter luôn trả `usage.cost` trong response, nhưng thư viện agent bỏ trường đó và code OCR không ghi lại | Đang làm: ghi `usage.cost` vào span và bảng `llm_calls`, cộng thành `runs.llm_cost_usd` |

Số liệu thật của 12 khung có phụ đề (thang 576×1024) dùng làm dữ liệu kiểm thử: hộp model có chiều cao 39–61 px và nằm trong ±10 px của chữ thật (y 691–732) ở 10 khung; từ 6,0 s trở đi cả 10 khung đều trả `null` đúng lúc phụ đề kết thúc.

Quy tắc rút ra cho eval: không tin test đơn vị với khung tổng hợp cho phần này; mọi thay đổi ở OCR (model, prompt, quy ước toạ độ, tắt suy luận, cách dựng hộp) phải chạy lại bước 6 trên khung thật trước khi coi là xong.

## Risk

5 video chỉ đủ chứng minh quy trình chạy được, chưa phải benchmark.

## Rollback

Chỉ thêm span và score, không đổi hành vi của workflow.

## Tiến độ: chi phí LLM thật (làm sớm)

Làm trước phần còn lại của phase này vì chi phí thật quan trọng ngay: một run thật gọi 4 request Localizer, 5 generation của Reviewer và 55 lần OCR, mà Langfuse chỉ có token (`costDetails = {}`, `calculatedTotalCost = 0`) và history Temporal có `usage.cost = null`.

- OpenRouter trả `usage.cost` (USD) và `usage.cost_details` trong mọi chat completion, không cần xin, cùng id generation (`gen-...`) ở đầu body.
- Một client HTTP duy nhất cho OpenRouter (`providers/llm.py: openrouter_client`) có response hook (`observability/llm_usage.py: make_response_hook`), dùng cho cả OCR (httpx trực tiếp) và model của các agent. Với thư viện agent: `OpenAIProvider(http_client=...)` nhận client httpx, nên hook trên client đó là đường lấy body thô, không cần fork thư viện. Thư viện ghi cảnh báo `httpx.AsyncClient` support for OpenAI-compatible providers is deprecated (bỏ ở v3, dùng `httpx2`); khi nâng lên v3 phải đổi client.
- Hook chỉ đọc reply JSON không phải stream, bằng `await response.aread()` (httpx giữ body để consumer đọc lại), rồi đặt lên span hiện tại các attribute Langfuse v3 nhận, đã kiểm trên Langfuse đang chạy qua OTLP và đọc lại bằng API công khai: `langfuse.observation.type = "generation"`, `langfuse.observation.model.name`, `langfuse.observation.usage_details` (JSON `{"input", "output", "total"}`), `langfuse.observation.cost_details` (JSON, khoá `total` là tổng; khoá khác như `upstream_inference_cost` được giữ nhưng không cộng thêm). Trên span thường (không đặt `type = generation`) Langfuse bỏ chi phí, nên type là bắt buộc. Kết quả đọc lại: `type GENERATION`, `costDetails {"total": 0.00123}`, `calculatedTotalCost 0.00123`. Các trace thăm dò (`probe-A/B/C`) được tạo bởi lần kiểm này vẫn còn trong Langfuse; không xoá.
- Hook không ghi prompt, ảnh, header hay khoá: chỉ id, tên model, số token và chi phí. Mọi lỗi trong hook bị nuốt, vì ghi sổ không được làm hỏng request.
- Bảng `localization.llm_calls` (khoá chính là id generation của OpenRouter; `aweme_id`, `generation`, `stage` là tên activity, `model`, token, `cost_usd`, `created_at`) do hook ghi qua `LlmCallRecorder` (best effort, ghi log chỉ kiểu lỗi, quá 5 giây thì bỏ; `ON CONFLICT DO NOTHING` nên một response ghi hai lần chỉ tính một). Video và generation lấy từ workflow id `video-{aweme}-g{n}` của activity đang chạy; ngoài activity thì không ghi bảng nhưng vẫn gắn chi phí lên span.
- `localization.runs.llm_cost_usd` là SUM theo (aweme_id, generation), ghi cùng lúc `publish` ghi dòng run (upsert nên idempotent; NULL khi không request nào báo chi phí).
- Migration: `services/localization/migrations/261027-llm-cost.sql` và `.down.sql`. **Chưa áp lên database nào**: cần áp (sau migration core, sau `pg_dump`) trước khi triển khai code này, vì `publish` đọc `localization.llm_calls`.
- Không có bảng giá trong code. Nếu một call vẫn thiếu chi phí thì nó không có chi phí. Một mục giá model trong Langfuse có thể làm dự phòng tính từ token, nhưng chưa làm.
- Mutation `localization:09` (10 mutation) đã đăng ký, chưa chạy.
