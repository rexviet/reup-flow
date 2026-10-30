---
phase: 9
title: "Score và eval trên Langfuse"
status: in-progress
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
| 6 | Hộp che quá to | Hộp cao 219 px (g6) và vùng phụ đề cao 246 px (g8) trong khi chữ cao khoảng 80 px. Lề 30% và 3%, phóng thêm 6%, hợp của mọi khung đều cộng dồn | Đã sửa cùng dòng 7 (`3e14cfc`): lề còn 13% chiều cao và 2% chiều rộng |
| 7 | Khớp điểm ảnh làm xấu đi trên khung thật | 7 trên 12 khung ra hộp cao 78–118 px thay vì 39–61 px của model, vì vạch trắng đường dành cho người đi bộ và đèn bị coi là chữ. Hai khung ngoại lai (y 860–924 và 770–852) kéo vùng hợp lên; chia theo mười phần chiều cao còn cắt đôi một dòng chữ | Đã sửa (`3e14cfc`): bỏ khớp điểm ảnh, vùng là đồng thuận (trung vị) trên hộp của model |
| 8 | Hộp che mờ 92% vẫn để lộ chữ | Chữ trắng viền đen tương phản mạnh nên vẫn đọc được xuyên qua hộp; Reviewer báo đúng | Đã sửa cấu hình: `COVER_OPACITY=1` |
| 9 | Chi phí LLM không hiện trong Langfuse | `GENERATION` có token nhưng `calculatedTotalCost = 0`. OpenRouter luôn trả `usage.cost` trong response, nhưng thư viện agent bỏ trường đó và code OCR không ghi lại | Đã sửa (`f6983dd`): `usage.cost` lên span và bảng `llm_calls`, cộng thành `runs.llm_cost_usd` |

Số liệu thật của 12 khung có phụ đề (thang 576×1024) dùng làm dữ liệu kiểm thử: hộp model có chiều cao 39–61 px và nằm trong ±10 px của chữ thật (y 691–732) ở 10 khung; từ 6,0 s trở đi cả 10 khung đều trả `null` đúng lúc phụ đề kết thúc.

Quy tắc rút ra cho eval: không tin test đơn vị với khung tổng hợp cho phần này; mọi thay đổi ở OCR (model, prompt, quy ước toạ độ, tắt suy luận, cách dựng hộp) phải chạy lại bước 6 trên khung thật trước khi coi là xong.

## Lỗi cần eval, gặp khi chạy 10 video thật (2026-10-28)

Nguồn: 10 video của một kênh (12 đến 84 giây) chạy cùng lúc với code ở commit `5b15fd9`: 5 `published`, 2 `needs_human`, 3 `failed`, tổng chi phí LLM khoảng $0,062. Bảng dưới chỉ ghi lỗi mà test đơn vị không đủ để bắt, vì kết quả phụ thuộc vào model hoặc dữ liệu thật. Hai lỗi chỉ cần test đơn vị (demucs bị kill `status -9` bị xếp là lỗi vĩnh viễn; hằng số `UNFIXABLE_ROOM_MS = 1500` mâu thuẫn với slot tối thiểu 1000 ms) không ghi ở đây.

| # | Lỗi | Bằng chứng | Chấm bằng | Chỉ số |
|---|---|---|---|---|
| E1 | Nhạc có lời hát bị coi là lời nói, rồi dịch và lồng tiếng đè lên bài hát | `7565005737722907953`: STT trả "Mmm, like getting" (5,2 đến 8,6 s) và "engulfed." (9,2 đến 28,0 s, một từ kéo 18,9 s), ngôn ngữ `en`. `7552039043057356090` nghi cùng dạng: "Thank / you." ở giây 16 và 20. Chưa sửa | Code | `false_dub_rate` (video không có lời nói mà vẫn bị lồng tiếng), `missed_speech_rate` (video có lời nói mà bị bỏ lồng tiếng) |
| E2 | Không lồng tiếng thì mất luôn tiếng gốc | `assemble_audio.py` chỉ trộn `bgm` (nhạc nền đã tách giọng) với clip lồng tiếng; không có clip thì đầu ra chỉ còn `bgm`. 4 video xuất bản có transcript rỗng (`7574654380532917477`, `7609902814123485361`, `7526777921518062905`, `7518248980598639930`) đều ra như vậy. Chưa sửa | Code | `original_vocals_kept`: năng lượng của kênh vocals nguồn có mặt trong âm thanh đầu ra ở video không lồng tiếng |
| E3 | Hộp che đặt sai chỗ, chữ Hán vẫn lộ | `7518248980598639930`: vùng phụ đề do OCR trả là `x 346, y 362, w 610, h 323` (giữa khung, đè lên người) và cue bắt đầu 7500 ms, trong khi chữ Hán thật nằm ở y khoảng 1110 (ước lượng từ ảnh chụp, chưa đo chính xác) và đã có từ giây 2. Hộp thô của model trên từng khung không được lưu nên chưa biết lỗi nằm ở model hay ở bước đồng thuận | Code | Mở rộng bước 6: thêm `region_hit` (hộp che cuối chứa hộp nhãn tay) và `cue_start_error_ms`; ghi lại hộp thô theo khung để dựng lại được |
| E4 | Reviewer cho qua cả E1 lẫn E3 | `7518…` được `published` với điểm `visual` 1.0 dù hộp nằm sai chỗ; `7565…` bị phát hiện chỉ vì lỗi fidelity chứ không vì lồng tiếng đè nhạc. Jev chỉ nhận văn bản; `residual_source_text` dùng cùng loại OCR nên có thể cùng sai | Code | Gieo lỗi đã biết vào render (hộp lệch chỗ, lồng tiếng đè nhạc, chữ Hán còn nguyên) rồi đo tỉ lệ Reviewer bắt được (`seeded_defect_recall`) và tỉ lệ báo nhầm trên render sạch |
| E5 | Localizer bỏ tên riêng hoặc mệnh đề khi tắt reasoning | `7575765838708520689`: "打赏支持明镜与点点栏目" bị dịch thành "Ủng hộ Minh Kính", mất 点点栏目. Ngưỡng Jev 0,8 chỉ dựa trên 9 dòng thật | Code và judge | Code: `glossary_term_hit` (thuật ngữ có trong glossary phải xuất hiện đúng trong bản dịch). Judge (`fidelity`, `JUDGE_MODEL`): chỉnh ngưỡng Jev bằng nhãn người, so `single` với `tools` và `jev` với `llm` |
| E6 | Một câu quá dài làm chết cả video sau khi đã tốn tiền | `7616569292510948709` (67 s, $0,0132) và `7618513827751629177` (84 s, $0,0161): `TextTooLong`, vì `MAX_METADATA_TEXT_BYTES = 1400` cho chuỗi base64 của một câu và lỗi `non_retryable`. Chưa biết câu cụ thể | Code | `text_too_long_rate` trên bộ video dài (mục tiêu 0) và `max_line_bytes` so với giới hạn |

**Hành vi mong muốn cho E1 (người dùng quyết định 2026-10-28):** video chỉ có nhạc có lời hát thì không dịch, không phụ đề, không lồng tiếng và giữ nguyên lời bài hát (tiếng gốc). Không dùng ngôn ngữ mà STT nhận dạng làm tiêu chí nhận ra nhạc: trong 5 transcript hiện có (757, 766, 7664 là `zh`; 7552, 7565 là `en`) nó chỉ trùng hợp, và một bài hát tiếng Trung sẽ lọt qua. Cách nhận ra nhạc chưa quyết định; ứng viên là đặc điểm cao độ của kênh vocals, thời gian của từ, độ tin cậy STT (`providers/openai_stt.py` đang bỏ các trường này), và mô hình phân loại. Phải chọn bằng một spike trên dữ liệu có nhãn, không chọn theo cảm tính.

**Lưu ý cho `cover_tightness` (bước 6):** ngưỡng "hộp một dòng cao ≤ 130 px" không đúng với chữ lớn. Video `7664547853368951397` có tiêu đề vàng hai dòng cao khoảng 190 px, hộp che 289 px (tỉ lệ khoảng 1,5), người dùng đã xem bản gốc và xác nhận chữ Hán thật sự to, video đạt. Chỉ số phải so với chiều cao chữ nhãn tay, không so với số pixel tuyệt đối.

**Sinh thêm ca tổng hợp (synthetic cases).** Bộ nhãn khởi đầu chỉ có 10 video, không đủ phủ các lỗi E1 đến E6. Dùng skill `/Users/vietphung/Data/ai-eval-course/cartwheel-homeworks/scenarios/skill/SKILL.md` (tên `synthetic-scenarios`) để sinh thêm ca. Quy trình của skill: đọc spec rồi đề xuất các chiều và giá trị cho người duyệt trước khi sinh; sinh tuple từ các chiều đã duyệt, không liệt kê hết tích Descartes mà lấy mẫu có chủ ý các tổ hợp hiếm và rủi ro; tách nhóm `coverage` (phủ đều mọi giá trị) khỏi nhóm `challenge` (khó nhưng hợp lệ); tính kết quả mong đợi từ nguồn đáng tin (SQL, hàm, tài liệu chính sách, bảng chất lượng dữ liệu), không dùng lời của model; ca nào code không xác định được thì đánh dấu để người chấm; ghi JSONL, mỗi ca một dòng, rồi chạy bộ kiểm hợp đồng trước khi gọi model.

Skill viết cho agent hỗ trợ khách hàng Cartwheel (các chiều gốc là vai trò, ý định, bản ghi, chính sách, số lần gọi tool, độ khó; ca gồm `opening_message` và `followups`). Với pipeline này phải đặt lại các chiều và định dạng ca, và nguồn sự thật là nhãn người gán hoặc hàm tính trong code, không phải cơ sở dữ liệu. Các chiều có thể đề xuất để người duyệt (chưa quyết định):
- loại âm thanh: lời nói, hát có lời, chỉ nhạc, im lặng, lời nói đè lên nhạc nền;
- độ dài video và độ dài một câu (liên quan trực tiếp tới E6);
- có phụ đề cháy sẵn hay không, kiểu chữ (trắng viền đen, có màu, tiêu đề lớn như `7664547853368951397`), vị trí (cuối khung, giữa khung, trên cao);
- vị trí câu cuối so với hết video (liên quan tới các ca `needs_human` đã gặp);
- có tên riêng và thuật ngữ trong glossary hay không (liên quan tới E5).

Giới hạn cần biết trước: skill sinh ca dạng văn bản. Các ca có âm thanh hoặc khung hình (E1 đến E4) cần thêm bước dựng đầu vào (ghép âm thanh, vẽ phụ đề lên khung nguồn không có phụ đề, hoặc tìm video thật), và bước đó nằm ngoài skill. Nhãn hộp chữ cho E3 vẫn phải do người vẽ trên khung thật, theo quy tắc của bước 6. Với nhóm `challenge`, chọn ca vì lý do khó đã nêu trước, không chọn chỉ vì model từng làm sai đúng ca đó.

**Bộ nhãn khởi đầu (10 video, nhãn do người gán):**

| Video | Dài | Cần nhãn | Ghi chú |
|---|---|---|---|
| `7565005737722907953` | 28 s | hát có lời | mẫu dương cho E1 |
| `7552039043057356090` | 21 s | cần nghe: hát hay nói | nghi là lời hát |
| `7575765838708520689`, `7665948504426075877`, `7664547853368951397` | 10, 23, 24 s | nói thật (zh) | mẫu âm cho E1, mẫu cho E5 và E3 (7664: hộp đúng) |
| `7574654380532917477`, `7609902814123485361`, `7526777921518062905` | 12, 16, 18 s | cần nghe: im lặng hay có hát | transcript rỗng, kiểm E2 |
| `7518248980598639930` | 23 s | cần nghe, và nhãn hộp chữ Hán | mẫu dương cho E3 |
| `7616569292510948709`, `7618513827751629177` | 67, 84 s | nói thật, dài | mẫu cho E6 |

Ba video rỗng ở trên chưa ai kiểm vị trí hộp che; nhãn hộp phải do người vẽ trên khung thật, theo quy tắc của bước 6.

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

## Tiến độ: score, span, dataset, experiment, eval (2026-10-28)

**Đã làm và đã kiểm:**

| Bước | Ở đâu | Bằng chứng |
|---|---|---|
| 1, 2. Score trên trace của run | `observability/scores.py`; `publish` gửi sau khi ghi dòng run. `PublishRequest` có thêm `dub` và `safety_flag` (workflow truyền vào; replay cũ vẫn xanh vì Temporal không so input của activity) | `tests/observability/test_run_scores.py`; gửi thật lên Langfuse đang chạy: được nhận, gửi lại thì ghi đè |
| 3. Span provider | `observability/provider_spans.py`; `provider.{stt,tts,ocr}` mang `provider`, `model`, `input_units` (giây âm thanh, số ký tự, số ảnh), bỏ `http.path` | `tests/observability/test_provider_spans.py`; canary cũ vẫn xanh |
| Test trace | `tests/observability/test_trace_completeness.py`: workflow thật, provider thật nói với stub server | Đủ `ingest`, `provider.stt`, `provider.ocr`, model request của Localizer, `provider.tts`, `review_render`, `publish` trong một trace; control: client không có interceptor thì rời ra |
| 4. Dataset | `eval/build_dataset.py` đọc history qua API của Temporal UI (gRPC 7233 không mở ra host) | `localizer-v1` có 5 video: 7575, 7665, 7664, 7552, 7565. Chỉ 2 run `published` có lời nói, nên lần này dùng `--status published,needs_human` |
| 5. Experiment | `eval/run_experiment.py`, `eval/evaluators.py`. Localizer chạy đúng luồng production (`localizer_single.localize`, tách ra từ `agents/temporal.py`) | Hai dataset run trên Langfuse, mỗi run 5/5 item có `fit_ratio`, `fidelity`, `naturalness` (AC8, kiểm qua API) |
| 6. Eval OCR | `eval/ocr_eval.py`, có `region_hit` và `cue_start_error_ms` (E3). `subtitles.json` giờ lưu hộp thô của từng khung (`frame_boxes`, `unusable_frames`) | `tests/eval/test_ocr_eval.py`. **Chưa chạy thật: chưa có nhãn hộp do người vẽ** |
| E1, E2, E6 | `eval/pipeline_eval.py` (`runs`, `vocals`) | Xem kết quả bên dưới |
| E5 (phần code) | `glossary_term_hit` trong `evaluators.py` | Có trong hai dataset run |

**Kết quả experiment** (prompt `localizer-1`, reasoning off, judge `deepseek/deepseek-v4-pro`; 5 video là quá ít để kết luận, xem Risk):

| Model | fit_ratio | fidelity | naturalness | glossary_term_hit | Chi phí (gồm judge) |
|---|---|---|---|---|---|
| `deepseek/deepseek-v4-pro` | 1.00 | 0.90 | 0.80 | 0.25 (1 video) | $0.036 |
| `deepseek/deepseek-v4.1-flash` | 0.95 | 0.90 | 0.75 | 0.50 (1 video) | $0.022 |

Cả hai đều bị judge chấm `fidelity` 0.5 trên 7575, đúng video có lỗi E5.

**Kết quả `pipeline_eval runs`** trên 6 video mà bảng nhãn ở trên đã ghi chắc (7565 hát; 7575, 7665, 7664, 7616, 7618 nói): `false_dub_rate` 1.0 (7565), `missed_speech_rate` 0, `text_too_long_rate` 2/6. Nguyên nhân E6 đã rõ: ở 7616 và 7618 **cả transcript là một segment**, câu dài 2.220 và 2.996 byte so với giới hạn 1.400. 7665 và 7664 cũng chỉ có một segment.

**Sửa lại E2:** đo `original_vocals_kept` trên bốn video transcript rỗng thì kênh vocals của nguồn gần như câm (RMS 8 đến 51, nhạc nền 4.000 đến 13.000 trên thang 32.767). Bốn video đó nhiều khả năng vốn không có giọng, nên chưa có bằng chứng E2 làm mất tiếng thật. Muốn đo E2 cần một video có hát và không bị lồng tiếng, tức là sau khi sửa E1. Vẫn cần người nghe để gán nhãn bốn video này.

**Lỗi do kiểm thật bắt được:** Langfuse từ chối (HTTP 400) một score vừa có `traceId` vừa có `sessionId`. Test với stub server đã không bắt được; đã bỏ `sessionId` và thêm assert.

**Lưu ý khi triển khai:** `Contract` từ chối field lạ, nên worker cũ không đọc được `PublishRequest` mới (có `dub`, `safety_flag`) hay `subtitles.json` mới (có `frame_boxes`). Build và khởi động lại cả bốn service (`worker-agents`, `worker-media`, `worker-render`, `localization-starter`) cùng lúc, như quy trình chạy thật vẫn làm; không để worker cũ và mới chạy song song. Payload và history cũ thì code mới đọc được.

**Review độc lập** (một agent khác đọc diff): không có lỗi chặn, không rò rỉ, không mất tính deterministic. Đã sửa theo review: score chỉ gửi khi span hiện tại có cha (workflow khởi động không có trace thì mỗi lần thử `publish` là một trace riêng, score sẽ lạc và nhân đôi khi retry); bọc toàn bộ phần chấm điểm trong `publish` để không bao giờ làm hỏng run; một test cũ đúng cả khi không gửi gì.

**Chưa làm:**
- Chạy `ocr_eval.py` trên khung thật: cần người vẽ nhãn (ít nhất 30 khung từ 5 clip). `ocr_eval.py frames` xuất khung để vẽ.
- E4 `seeded_defect_recall`: cần dựng render có lỗi gieo sẵn qua worker render; chưa thiết kế.
- E5 chỉnh ngưỡng Jev bằng nhãn người; so `single` với `tools` (experiment hiện chỉ chạy `single`).
- Nhãn âm thanh cho 7552 và bốn video transcript rỗng; ca tổng hợp bằng skill `synthetic-scenarios` (cần người duyệt các chiều trước).
- AC7 trên một run thật: kiểm được một phần (2026-10-29), chưa xong.
  - Phần đã kiểm: score nằm trên trace của chính run generation 2, không lẫn sang trace generation 1.
    - 7616: `needs_human`, `safety_flag`, `review_rounds`, `mean_stretch`, `overflow_rate`, `residual_han_cues`.
    - 7618: `needs_human`, `review_rounds`, `safety_flag`.
  - Phần chưa kiểm: `fidelity`, `timing`, `visual` của Reviewer chưa xuất hiện trên run thật nào.
    - 7616 thiếu chúng vì bước review hết hạn heartbeat.
    - 7618 fail vì Colab mất kết nối (TTS trả HTTP 530 ở vòng chạy lại thứ hai), nên không có verdict.
  - Chụp màn hình cho `reports/` chưa làm. AC8 đã kiểm bằng API.
- Review của 7616 hết hạn heartbeat (60 s) hai lần, cả hai lần đều lúc đang chờ OCR trả lời. Nguyên nhân chưa chứng minh được.
  - Lỗi chỉ xảy ra trong lúc 7618 đang render; render đó mất 13,6 phút thay vì khoảng 4 phút.
  - Health check định kỳ 30 s của worker có một khoảng trống 73 s.
  - Giả thuyết: event loop của `worker-agents` bị nghẽn khi máy quá tải. Tách heartbeat ra khỏi lần gọi OCR hoặc tăng `heartbeatTimeout` chỉ nên làm sau khi tìm được nguyên nhân.
- Mutation `localization:09` đã đăng ký thêm 10 cái cho phần này, chưa chạy (chạy một lần sau phase 10).

**Cách chạy lại** (từ `services/localization`, stack compose đang chạy):

```
uv run python eval/build_dataset.py --videos 5 --status published,needs_human
uv run python eval/run_experiment.py --model <id> --prompt-version localizer-1
uv run python eval/pipeline_eval.py runs --labels labels.jsonl
uv run python eval/ocr_eval.py run --labels labels.json --model <id> --bbox-format xyxy_norm1000 --out report.json
```

## Ca tổng hợp và baseline của Localizer (2026-10-28)

Sinh bằng quy trình của skill `synthetic-scenarios`, script `eval/synth_scenarios.py`; ca nằm ở `eval/scenarios/localizer-synth-v1.jsonl`, trên Langfuse là dataset `localizer-synth-v1`. Người dùng duyệt sáu chiều: độ dài segment, độ chật của slot, tên riêng và glossary, số và chữ Latin, nội dung rủi ro, phụ đề cháy sẵn. Model chỉ viết lời thoại tiếng Trung theo yêu cầu, code kiểm lại và bỏ bản nháp sai. Slot, glossary và mọi kết quả mong đợi do code tính. Sinh được 41/55 ca (30 coverage, 11 challenge); 14 ca bị bỏ vì model viết sai yêu cầu ba lần. Hai lý do khó ("kiểu 7575" và "nhiều thuật ngữ, số, slot chật") chỉ còn 1 ca mỗi loại.

**Baseline** (run `localizer-1 deepseek/deepseek-v4-pro reasoning-off 261028-2210`, 41 ca, $0,505; judge `deepseek-v4-pro`, trần 8.192 token):

| Chỉ số | Coverage | Challenge |
|---|---|---|
| `expected_checks` | 0.89 | 0.80 |
| `line_bytes_within_limit` | 0.63 | 0.45 |
| `glossary_terms_used` | 0.94 | 0.78 |
| `numbers_and_latin_kept` | 0.95 | 0.67 |
| `no_new_contact`, `no_unusable_lines` | 1.00 | 1.00 |
| `fidelity` / `naturalness` (judge) | 0.78 / 0.66 | 0.61 / 0.55 |

Lỗi tập trung: 17/19 ca một segment dài cả video vượt 1.400 byte (E6); 3/7 ca lời kêu gọi ủng hộ ở câu cuối chật bỏ một thuật ngữ glossary (E5); 3 ca slot chật bỏ số, email hay số điện thoại.

**Nhãn người trên 12 trace** (score `human_fidelity`, `human_naturalness`, `human_injection_ok`): judge chấm fidelity thấp hơn người 0,5 điểm (thang 1 đến 5), khớp đúng 6/12, lệch không quá 1 điểm 11/12 (sau khi người dùng chấm lại 0052 thành 2 và 0028 thành 4, đồng ý với judge). Naturalness lệch 0,25 điểm, khớp đúng 5/12, lệch không quá 1 điểm 8/12. Không ca injection nào bị làm theo (6/6). Kết luận tạm: `fidelity` của judge dùng được để so hai run, `naturalness` chưa đủ tin để làm căn cứ đổi prompt.

**Quyết định của người dùng (2026-10-28):** bỏ một thuật ngữ glossary là lỗi, kể cả ở câu cuối chật. `glossary_terms_used` là điều kiện bắt buộc. Nhãn fidelity 5 của 0052 và 0028 được gán khi chưa để ý chỗ bị bỏ; người dùng đã chấm lại theo judge.

**Sửa trong phiên này:**
- `text/safety.find_contacts` đọc mất phần đầu số điện thoại đứng sau `，` (NFKC thành `,`, lookbehind của regex từ chối). Quy tắc production `contacts_missing_from_source` so theo chữ số nên không bị ảnh hưởng; check eval `no_new_contact` từng so trực tiếp hai danh sách và chấm sai, đã đổi sang quy tắc production.
- E6: STT trả mỗi chữ Hán một "từ" liền nhau, khoảng cách giữa từ gần 0, nên quy tắc cắt 400 ms không bao giờ chạy và cả video thành một segment (7616, 7618, 7665, 7664). `regroup` giờ cắt mỗi segment tối đa 60 chữ (tối thiểu 20), ưu tiên dấu câu, rồi khoảng lặng lớn nhất, rồi chữ bị kéo dài nhất. Chạy offline trên transcript thật: 7616 thành 7 segment, 7618 thành 12. Workflow không đổi. **Đã kiểm trên run thật (2026-10-29, generation 2):** 7616 có 7 segment, câu dài nhất 364 byte; 7618 có 11 segment, câu dài nhất 336 byte (giới hạn 1.400). Cả hai qua TTS và render, không còn `TextTooLong`.
- Judge có trần 8.192 token (trước đó xin 131.072 nên bị key giới hạn từ chối).

**Còn mở:**
- Số bị tách thành từng chữ số ("30岁" thành "3 0岁") trong `transcribe.py`.
- Khoảng lặng nằm trong một chữ kéo dài vẫn không tự kết thúc segment.
- `test_simultaneous_requests_for_different_texts_are_not_serialised` đỏ một lần khi máy bận, xanh 3/3 khi chạy riêng: test đo thời gian nên nhạy với tải.

**So sánh sau này:** giữ nguyên dataset (đổi ca thì làm v2); mỗi lần chỉ đổi model, prompt hoặc reasoning; đổi prompt thì đổi `--prompt-version`; giữ judge; đọc coverage và challenge riêng; chạy kèm `localizer-v1`; chênh 1 đến 2 ca chưa có nghĩa. Bản sửa E6 không đo được bằng dataset này vì experiment bỏ qua bước transcribe.

### Sửa E5 bằng code (2026-10-29)

`localizer_single.assess` giờ coi một câu bỏ thuật ngữ glossary mà câu gốc nói là có lỗi; câu đó được gửi lại trong request sửa duy nhất, kèm ghi chú tên thuật ngữ và bản dịch của nó. Sửa xong vẫn thiếu thì câu được giữ, run không fail. Check nằm sau marker `localizer-glossary-terms`; history mới ở `tests/replay/histories/glossary-terms/`. Run eval mang nhãn `localizer-2`.

So với baseline trên `localizer-synth-v1` (run `localizer-2 deepseek/deepseek-v4-pro reasoning-off 261028-2345`, 41 ca, $0,544 so với $0,505):

| Chỉ số | Coverage | Challenge |
|---|---|---|
| `glossary_terms_used` | 0.94 → 0.94 | 0.78 → **1.00** |
| `expected_checks` | 0.89 → 0.88 | 0.80 → 0.85 |
| `fidelity` (judge) | 0.78 → 0.87 | 0.61 → 0.68 |
| `naturalness` (judge) | 0.66 → 0.65 | 0.55 → 0.50 |
| `fit_ratio` | 0.91 → 0.84 | 0.73 → 0.87 |

Ca "kêu gọi ủng hộ": 3/7 trượt còn 1/7. Ca còn trượt là 0028: sau request sửa, model giữ "Điểm Điểm" nhưng bỏ "chuyên mục", nên câu vẫn thiếu thuật ngữ đầy đủ; judge cũng trừ điểm vì mất "打赏". Các chênh lệch khác là 1 đến 2 ca, chưa đủ để kết luận; `fit_ratio` của coverage giảm cần theo dõi ở run sau. Lần chạy đầu dừng ở ca 41 vì nhà cung cấp `deepseek-v4-pro` trả 429; ca đó đã được chạy tiếp vào cùng run.

Quyết định ngày 2026-10-29:
- **Giữ tên riêng là đạt.** Với thuật ngữ có chữ chức danh, câu chỉ cần giữ tên riêng: giữ "Điểm Điểm" mà bỏ "chuyên mục" vẫn đạt. Check `glossary_terms_used` đọc tên đó từ `names` trong spec của case. Danh sách nằm ở `synth_scenarios.TERM_NAMES`: `点点栏目` → "Điểm Điểm", `星辰频道` → "Tinh Thần". 14 case được thêm `names`.
- **Chỉ đổi phần chấm.** Input của Localizer giữ nguyên, nên run cũ và mới vẫn so được với nhau. Hai run đã được chấm lại từ output đã lưu, không gọi model. Kết quả:
  - `glossary_terms_used` của coverage: 0.94 → 1.00 ở cả hai run.
  - Challenge: `localizer-1` vẫn 0.78 (0052 và 0054 trượt); `localizer-2` vẫn 1.00.
  - Không còn ca "kêu gọi ủng hộ" nào trượt ở `localizer-2`.
- **Hai chỗ chưa đổi theo quyết định này:**
  - Điểm `glossary_term_hit` vẫn đòi đủ cả cụm.
  - Bước sửa trong production (`missing_terms`) vẫn gửi request sửa khi thiếu "chuyên mục". Request này chỉ tốn thêm một lượt gọi, không làm run fail.
- **Đổi tên prompt.** `PROMPT_VERSION` trong code là `localizer-2`.
- **Hoãn lỗi "3 0岁".** Lỗi tách số trong `transcribe.py` để sau.
