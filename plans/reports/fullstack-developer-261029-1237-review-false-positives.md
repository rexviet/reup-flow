# Sửa hai kiểm tra review báo nhầm `needs_human`

Trạng thái: DONE_WITH_CONCERNS. Chưa commit.

## Nguyên nhân gốc

Kiểm tra "source text is burned in next to the caption" chạy OCR trên video đã render. Trên video đó, phụ đề tiếng Việt của chính mình nằm trên hộp che, nên OCR tìm thấy nó. Hộp OCR vẽ lại rộng hơn hộp phụ đề: ở 7618, hộp OCR là y1455 h164 còn hộp che là y1455 h125, lòi xuống dưới 39 px. Biên 15% của `residual_findings` không đủ, nên phụ đề của mình bị coi là phụ đề gốc còn sót. Kiểm tra "the line is read too fast" thì so với `FAST_RATIO = 1.12` chép tay, trong khi khâu lồng tiếng được phép kéo tới `STRETCH_MAX = 1.15`. Vì vậy review từ chối những câu mà Dubber được phép tạo ra (7616 có một câu ở x1.13).

## Thay đổi

- `services/localization/src/localization/agents/review_checks.py`
  - `FAST_RATIO = STRETCH_MAX` (import từ `contracts.models`), không còn số chép tay. Câu chỉ bị báo "too fast" khi đúng ở giới hạn 1.15, tức câu Dubber phải bỏ lại ở giới hạn. Comment đã viết lại cho đúng. `SLOW_RATIO = 0.88` giữ nguyên.
  - `residual_findings(spec, subtitles: SubtitleDocument)` mới. Hàm không dùng OCR trên render mà dùng `frame_boxes` mà `detect_subtitles` đã lưu cho video gốc (mỗi 500 ms một hộp). Một hộp bị coi là "ngoài hộp che" khi hơn 50% diện tích của nó không bị che. Các hộp ngoài hộp che được gom thành dải theo hàng (chồng nhau theo chiều dọc ≥ 50% hộp thấp hơn), rồi lấy dải lớn nhất. Hàm chỉ báo khi dải đó có mặt trên ít nhất 1/3 số frame có hộp (`RESIDUAL_SHARE = Fraction(1, 3)`) và kéo dài ít nhất 2 s (`RESIDUAL_MIN_MS`). Hộp được đổi sang pixel của spec nếu kích thước tài liệu khác kích thước video. Văn bản finding, dimension `visual` và stage `render` giữ nguyên định dạng. Hộp được báo là trung vị trên/dưới của dải, ghép rộng theo chiều ngang. Finding nêu tối đa 4 thời điểm. Không có cover, hoặc tài liệu cũ không có `frame_boxes`, thì không kiểm tra gì.
  - Đã bỏ `residual_times`, `_gap`, `RESIDUAL_DISTANCE`, `RESIDUAL_FRAMES`.
- `services/localization/src/localization/activities/detect_subtitles.py`: thêm `subtitles_key(aweme_id)`, là cùng `work_key(aweme_id, 0, "subtitles.json")` mà activity vẫn dùng để ghi, và activity giờ gọi qua hàm này.
- `services/localization/src/localization/activities/review_render.py`: ở chế độ `jev`, khi spec có cover thì đọc `SubtitleDocument` tại `subtitles_key(spec.aweme_id)` (`produced_by="detect_subtitles"`) và không gọi OCR trên render nữa. `aweme_id` được truyền xuống `_review_with_jev` và `_judge`. `LocalVideo.find_text` vẫn còn cho tool `residual_source_text` của chế độ `llm`. Docstring module đã cập nhật.
- Test:
  - `tests/agents/test_review_checks.py`: test ở dạng thật của dữ liệu, gồm khung dọc 1080x1920, cover là hộp phụ đề hai dòng của 7618, hộp mỗi 500 ms, một frame lệch, các frame unusable. Có regression 7618 (hộp x54 y1455 w972 h164 trên 160 frame thì không báo). Có ca phụ đề thật nằm dưới cover trên 40 frame thì vẫn báo. Có ca nhiễu giống 7616 (22%, 8 frame liền) thì không báo. Có biên ngưỡng 1/3 (báo đúng ở ngưỡng, không báo khi thiếu một frame), độ dài tối thiểu, đổi tỉ lệ pixel, không cover hoặc tài liệu cũ. Test tốc độ dùng `STRETCH_MAX`, và x1.13, x1.149 đều qua.
  - `tests/activities/test_review_render.py`: fixture có thêm tài liệu phụ đề, kèm helper `put_subtitles`.
  - `tests/activities/test_review_render_jev.py`: phụ đề sót được tìm từ tài liệu và OCR không được gọi. Chữ quanh phụ đề trên render không bị báo. Không cover thì không cần tài liệu. Có cover mà mất tài liệu thì báo `ArtifactMissingError` của `detect_subtitles`.
  - `tests/workflows/test_prep_stage.py`: khẳng định `detect_subtitles` trả về đúng `subtitles_key(aweme_id)`.

## Workflow có đổi không

Không. `video_localization.py` không đổi, tham số activity giữ nguyên, thứ tự command giữ nguyên, nên không cần `workflow.patched` hay history replay mới. Review tự tìm tài liệu bằng key mà `detect_subtitles` dẫn xuất từ `aweme_id`. Workflow chạy lại `detect_subtitles` (khi tài liệu mất) cũng ghi lại vào cùng key đó.

Nếu tài liệu mất lúc review, activity ném `ArtifactMissingError` (non-retryable). Workflow coi đó là "the review could not be made" và giữ video lại cho người xem, không publish.

## Kết quả trên video thật

Tôi lấy mọi `work/*/r*/subtitles.json` và `edit-spec.json` từ MinIO qua container `worker-media` (không in credential), rồi chạy check mới offline:

| Video | Spec | Kết quả |
|---|---|---|
| 7616569292510948709 | r0, r1 (có cover, 119/134 frame có hộp) | không báo gì |
| 7618513827751629177 | r0, r1 (có cover, 163/168 frame có hộp) | không báo gì |
| 7575765838708520689 | r0, r1 (có cover, tài liệu cũ không có `frame_boxes`) | không kiểm được, không báo |
| 7664547853368951397 | r0 (có cover, tài liệu cũ) | không kiểm được, không báo |
| 7518248980598639930 | r0 (có cover, tài liệu cũ) | không kiểm được, không báo |
| 7665948504426075877, 7526777921518062905, 7552039043057356090, 7565005737722907953, 7574654380532917477, 7609902814123485361 | không cover | không báo |
| 9917930455608654433, 9917930471099093907, 9917930623624784657 | không có tài liệu phụ đề, spec định dạng cũ | bỏ qua |

Các ngưỡng được chọn dựa trên số đo như sau. Hộp lưu trong `frame_boxes` nhiễu hơn nhiều so với tôi nghĩ. Ở 7616, 30% số frame có hộp nằm phần lớn ngoài cover, dải lớn nhất chiếm 22%, và có 8 frame liền (25,5–29 s) hộp nằm hẳn dưới cover. Tôi đã cắt frame gốc (t=27,5 s ở 7616; t=2 s ở 7618) và thấy dòng chữ Hán thật nằm trong cover, còn mô hình đặt hộp thấp hơn 140–270 px. Ở 7618, dải lớn nhất chiếm 9%. Vì vậy ngưỡng 1/3 chừa biên khoảng 1,5 lần so với ca tệ nhất đã đo.

Canary trên chính dữ liệu thật: tôi dịch cover của 7616 và 7618 lên 40, 80, 150 px, hoặc đẩy nửa sau các hộp xuống 160 px. Cả 8 trường hợp đều bị báo "left-over source subtitle".

## Lệnh test

- `uv run pytest -q tests/agents/test_review_checks.py tests/activities/test_review_render_jev.py tests/activities/test_review_render.py tests/workflows/test_prep_stage.py`: 101 passed.
- `uv run pytest -q tests/agents tests/activities tests/workflows tests/replay`: 819 passed, 2 deselected (7 phút 17 giây).
- Sau đó tôi sửa thêm một chỗ nhỏ trong `review_checks.py` (bỏ lọc `unusable_frames` thừa, vì frame unusable vốn đã là `None` theo contract). `uv run pytest -q tests/agents/test_review_checks.py tests/activities/test_review_render_jev.py`: 41 passed.
- `uv run ruff check` và `uv run ruff format --check` trên 7 file đã sửa: sạch.
- Đột biến thủ công (không dùng `tests/mutations/`): đổi `FAST_RATIO` thành 1.12, `RESIDUAL_UNCOVERED` thành 0.2, `RESIDUAL_SHARE` thành 1/5, `RESIDUAL_MIN_MS` thành 500, hoặc bỏ điều kiện độ dài dải. Mỗi đột biến đều làm ít nhất một test đỏ.

## Điểm còn lo

- Với độ nhiễu hiện tại của bộ dò, một cover chỉ để sót phụ đề trên dưới khoảng 1/3 số frame có chữ sẽ không bị bắt. Đó là cái giá để 7616 không bị báo nhầm. Bằng chứng mới chỉ có hai video có `frame_boxes`. Nên đo lại ngưỡng khi có thêm video, nhất là video có lỗi cover thật.
- Video có tài liệu cũ (7575, 7664, 7518) không có `frame_boxes`, nên check này không kiểm được gì cho chúng. Trước đây OCR trên render ít nhất còn nhìn vào video.
- Check này dựa trên giả định rằng cover vẽ ra đúng như spec. Lỗi ở khâu render (cover không được vẽ) không còn được kiểm bằng hình ảnh ở chế độ `jev`.
- `SLOW_RATIO = 0.88` vẫn chặt hơn `STRETCH_MIN = 0.85`. Tôi không đổi vì yêu cầu chỉ nói về ngưỡng nhanh.
- Controller cần rebuild `worker-agents`/`worker-media` để code mới có hiệu lực. Tôi không restart service nào.
