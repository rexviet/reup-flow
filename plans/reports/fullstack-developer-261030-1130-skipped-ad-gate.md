# Cổng bỏ qua video quảng cáo (`skipped_ad`)

## Kết quả

Cổng đã chạy trong `VideoLocalizationWorkflow`. Video được chuẩn bị xong (ingest, STT, dò phụ đề) thì Jev được hỏi một câu `promotes_product` trên chữ của transcript. Nếu xác suất >= ngưỡng (mặc định 0.8) thì run kết thúc ngay: `localization.runs.status = 'skipped_ad'`, không chạy Localizer, Dubber, synthesize, layout, render hay review, và không có render nào bị sao chép. Mọi trường hợp khác vẫn chạy như hôm nay: video không bán gì, video không có lời nói (khi đó không gọi Jev), cổng bị tắt, hay Jev hỏng hẳn (fail open).

Toàn bộ test của service đều xanh: 3094 passed, 7 deselected (là các test đánh dấu `live`). Replay cũng xanh: lịch sử cũ replay không đổi, và ba lịch sử mới (trong đó có nhánh skip) đều replay được. Đã kiểm tra thật với Jev qua OpenRouter: 7616 và 7618 bị bỏ qua, sáu video còn lại có lời nói vẫn được localize.

## Patch id

`AD_GATE_PATCH = "skip-advertisements"` nằm trong `workflows/video_localization.py`. Việc kiểm tra marker đi qua `_asks_about_ads()`, giống cách `_review_first()` làm, để một workflow "ungated" trong test có thể bỏ marker đi làm control.

## Các file đã đổi

Thư mục gốc là `services/localization/`.

- `src/localization/agents/ad_classifier.py` (mới): câu hỏi Noul `promotes_product` (lời văn chép đúng từ spike), `build_state()` bỏ các dòng trống và trả None khi không có chữ nào, và `is_advertisement(answers, threshold)`.
- `src/localization/activities/classify_video.py` (mới): `ClassifyVideoActivity.classify_video(spec, transcript) -> bool`. Activity trả False ngay nếu cổng tắt hoặc `segment_count == 0`. Nếu không, nó đọc transcript qua `read_transcript_document` (dùng lại hàm sẵn có), gọi Jev đúng một lần và ghi usage/cost dưới stage `classify_video` qua recorder sẵn có. Activity chỉ đọc chữ.
- `src/localization/workflows/video_localization.py`: thêm patch, thêm `CLASSIFY` (queue `llm`, start_to_close 2 phút, retry giống `REVIEW` là 2 lần, 10 s, không retry lỗi `_PERMANENT`), thêm `_is_advertisement` (bắt `ActivityError` và trả False, nên fail open), và thêm `_skip_ad`.
- `src/localization/contracts/models.py`: `PublishOutcome` có thêm `"skipped_ad"`; docstring được cập nhật.
- `src/localization/activities/publish.py`: `skipped_ad` được xử lý như `failed`, tức là từ chối khi có `render_key` và không sao chép gì. Run skip không gửi score lên Langfuse.
- `src/localization/config.py`: thêm `ad_gate_enabled: bool = True` và `ad_gate_threshold: float = 0.8` (ràng buộc 0..1). Cổng dùng lại `reviewer_jev_model` và `jev_base_url`, không có cấu hình Jev thứ hai.
- `src/localization/workers/run.py`: một client Jev `functools.cache` dùng chung cho review và ad check; `classify_video` được đăng ký trên queue `llm`.
- `.env.example` và `docker-compose.yml` (service `worker-agents`): thêm `AD_GATE_ENABLED` và `AD_GATE_THRESHOLD`, đặt cạnh `REVIEWER_JEV_MODEL`. File `.env` không bị đụng tới.
- Tests:
  - mới: `tests/support/ad_fakes.py`, `tests/activities/test_classify_video.py`, `tests/workflows/test_ad_gate.py`, `tests/replay/ungated_ad_gate_flow.py`, `tests/replay/histories/skip-advertisements/*.json` (3 file);
  - sửa: `tests/support/prep_worker.py` (đăng ký activity thật trên `llm`, mặc định "không phải quảng cáo"), `tests/replay/record_histories.py`, `tests/replay/test_replay.py`, `tests/activities/test_publish.py`, `tests/observability/test_run_scores.py`, `tests/workers/test_agent_wiring.py`, `tests/workflows/test_prep_stage.py`.

## Cách ghi skip và lý do

Skip được ghi bằng cách dùng lại activity `publish` với `PublishRequest(outcome="skipped_ad")`. Lý do:

- Activity này đã là nơi duy nhất ghi `localization.runs`, và đã có sẵn upsert idempotent, `llm_cost_usd` (gồm cả chi phí của chính lần gọi Jev này), retry policy `PUBLISH` và đường `_record_failure`.
- Phần phải đổi chỉ là thêm `skipped_ad` vào nhánh "không có render" và bỏ score. Không cần điều kiện gượng ép nào.

Row của một run skip có các giá trị: `status='skipped_ad'`, `output_key=NULL`, `review_key=NULL`, `needs_human=false`, `review_rounds=0`, `verdict={"reasons": [], "verdict": null}`.

Kết quả workflow trả về là `LocalizationResult(prep=..., script=None, dub=None, publish=PublishResult(outcome="skipped_ad"))`. Hình dạng field giữ nguyên, nên các result cũ vẫn hợp lệ.

Schema: `status` là `text NOT NULL` và không có CHECK (xem `migrations/261026-localization-core.sql`). Có một test trên Postgres thật với migration thật đã ghi `skipped_ad` thành công. Schema không đổi nên không cần backup.

## Ảnh hưởng tới score và những chỗ khác đọc outcome

- Langfuse score: run skip không gửi score nào. Nếu gửi `review_rounds=0` và `needs_human=0` thì sẽ đọc thành "video ra ngoài mà không ai review". Test `test_a_skipped_advertisement_sends_no_scores` khóa điều này. File `observability/scores.py` không cần đổi.
- `eval/build_dataset.py` lọc `status = ANY(...)`, mặc định `published`, nên run skip không lọt vào dataset. `eval/pipeline_eval.py` chỉ hiển thị status. Outbox starter không đọc status hay result.
- `VbeeDubbingWorkflow` dùng chung `PublishOutcome` nhưng không phát ra `skipped_ad`. Workflow này không có cổng, đúng như phạm vi đã giao.
- Không có test nào liệt kê đủ các outcome, nên không phải sửa test vì lý do đó.

## Test: lệnh và kết quả

Tất cả chạy trong thư mục `services/localization`.

- `uv run pytest -q tests/activities/test_classify_video.py`: 15 passed. Bao gồm: ngưỡng lấy từ settings (có cả trường hợp đúng bằng ngưỡng), cổng tắt thì không đọc gì, transcript không có segment hoặc chỉ có dòng trống thì Jev không được gọi, lỗi permanent được ném ra, mất transcript ra `ArtifactMissingError`, và ghi usage qua transport thật với stage `classify_video`.
- `uv run pytest -q tests/workflows/test_ad_gate.py`: 5 passed, chạy trên Temporal local, có kiểm tra danh sách activity được lên lịch trong history.
  - Video quảng cáo: các activity được lên lịch đúng bằng `{ingest, voice_rate, transcribe, detect_subtitles, classify_video, publish}`, không có `agent__*`, row là `skipped_ad`, không có render và không có review.
  - Bốn trường hợp còn lại (không bán gì, không có lời nói, cổng tắt, Jev lỗi permanent) đều kết thúc `published`, với `classify_video` được lên lịch đúng một lần, ngay sau phần chuẩn bị.
- `uv run pytest -q tests/activities/test_publish.py tests/observability/test_run_scores.py`: 31 passed (có Postgres scratch thật).
- `uv run pytest -q tests/workers`: 82 passed.
- `uv run pytest -q tests/replay`: 334 passed. Trong đó có control `UngatedAdGateWorkflow` (cổng không có marker) bị `NondeterminismError` trên mọi lịch sử cũ đã đi qua phần chuẩn bị.
- `uv run ruff check src tests` và `uv run ruff format --check src tests`: sạch.
- Toàn bộ: `uv run pytest -q -p no:randomly`: **3094 passed, 7 deselected**, 3 warning có sẵn từ trước, mất 637 s.

Một số test cũ phải sửa (không có test nào bị xóa hay nới lỏng):

- `test_replay.py`: thêm set `skip-advertisements` vào các danh sách "lịch sử ghi trước X" (rate cạnh ingest, one-request, glossary, even batches, review-first). Lần thêm `dubber-code-mode` trước đây cũng sửa đúng như vậy. Các assert trên lịch sử cũ giữ nguyên.
- `test_replay.py`: control `localizer_context` loại `advertisement-skipped`, vì run này không tới Localizer. Đây là lý do giống với `voice-rate-fails-for-good` vốn đã bị loại sẵn.
- `test_prep_stage.py::test_the_workflow_names_only_activities_that_are_registered`: thêm `classify_video` vào danh sách tên được ghim, nên test này chặt hơn.
- `prep_worker.py`: đăng ký thêm `classify_video`, nếu không thì mọi workflow test sẽ treo ở queue `llm`.

## Kiểm tra live với Jev thật (OpenRouter, `typesafe/jev-1.13`, ngưỡng 0.8)

Chạy `ClassifyVideoActivity` thật qua `ActivityEnvironment` trên 12 mục của `transcripts.json`. Script nằm ở scratchpad (`live_ad_gate.py`) và không in key.

| video | segs | promotes_product | quyết định |
|---|---|---|---|
| 7616569292510948709 | 7 | 0.99 | skipped_ad |
| 7618513827751629177 | 12 | 0.99 | skipped_ad |
| 7552039043057356090 | 2 | 0.01 | localize |
| 7565005737722907953 | 2 | 0.02 | localize |
| 7575765838708520689 | 3 | 0.03 | localize |
| 7652576736073102842 | 10 | 0.05 | localize |
| 7664547853368951397 | 3 | 0.03 | localize |
| 7665948504426075877 | 2 | 0.02 | localize |
| 7518…, 7526…, 7574…, 7609… | 0 | (không gọi Jev) | localize |

Mỗi lần gọi mất khoảng 0.5 đến 1.1 s.

## Đã xác minh và chưa xác minh

- **Đã xác minh:**
  - logic workflow trên Temporal local;
  - replay lịch sử cũ và mới;
  - publish trên Postgres thật (DB scratch);
  - quyết định trên Jev thật cho 8 transcript thật;
  - wiring worker theo settings.
- **Chưa xác minh:**
  - Chưa chạy video thật end-to-end. Chưa có worker thật nào chạy code mới, và VoiceStudio đang down.
  - Chưa chạy mutation harness, theo đúng quy định là để sau phase 10.
  - Chưa có test nào cho trường hợp lỗi transient hết lượt retry. Trường hợp này đi chung nhánh `except ActivityError` với lỗi permanent, mà nhánh đó đã có test, nhưng chưa có test riêng vì nó cần chờ thật 10 s backoff.

## Rủi ro

- Prompt injection: transcript là dữ liệu không đáng tin, nhưng Jev chỉ trả về một xác suất. Kịch bản xấu nhất là một quảng cáo bị localize, hoặc một video bình thường bị bỏ qua. Bỏ qua nhầm có thể sửa bằng cách chạy lại với `AD_GATE_ENABLED=false` và tăng `generation`.
- Mới đo trên 2 quảng cáo và 6 video không phải quảng cáo. Ngưỡng 0.8 cách xa cả hai nhóm (0.05 và 0.99), nhưng mẫu còn nhỏ. Các loại video ở vùng giữa (review sản phẩm không có ý bán, unboxing, livestream bán hàng) chưa được đo.
- Run skip vẫn trả tiền STT, Demucs và OCR, vì cổng đặt sau `_prepare` theo quyết định của user.
- Thông điệp log fail-open lặp lại loại lỗi, ví dụ "PermanentProviderError: PermanentProviderError: …". Đây là do hàm `_describe` có sẵn, chỉ ảnh hưởng tới log.
- Khi deploy, các run đang chạy dở không có marker nên sẽ đi tiếp như cũ. Chỉ run mới mới đi qua cổng.

## Câu hỏi chưa giải quyết

1. Có cần UI hay báo cáo nào liệt kê các run `skipped_ad` để người duyệt kiểm tra các trường hợp bỏ qua nhầm không? Hiện tại muốn xem thì phải query `localization.runs` trực tiếp.
2. `VbeeDubbingWorkflow` có cần cổng tương tự không? Lần này không làm vì nằm ngoài phạm vi.
