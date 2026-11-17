# Bước tìm phụ đề lấy frame thưa: OCR rẻ hơn 11–14 lần, nhanh hơn 15–24 lần

**Date**: 2026-11-17 13:38 (Asia/Saigon)
**Severity**: Medium
**Component**: services/localization — `detect_subtitles`, `complete_transcript`, eval
**Status**: Đã chạy thật trên workflow. Code và script eval đã commit.

## Kết quả

Bước `detect_subtitles` không còn hỏi model ở mọi frame 2 fps nữa. Nó lấy một lưới khoảng 8 frame, rồi chia đôi bước lưới cho tới khi vùng phụ đề trùng với vùng của một lưới thưa hơn. Lưới mịn nhất là một frame mỗi 4 giây, và video ngắn được lấy dày hơn. Bước `complete_transcript` giữ nguyên cách cũ: chỉ đọc chỗ có phụ đề mà không ai nói.

Chạy thật trên workflow với hai video tốn OCR nhất trong 44 bản đã render và duyệt. Mỗi workflow được dừng ngay sau `complete_transcript`.

| Video | Tiền OCR: g1 → mới | Thời gian OCR: g1 → mới | Box lệch |
|---|---|---|---|
| `7702770865292758299` (20 phút, g1 → g3) | 0,179 → 0,016 USD | 16,1 phút → 65 giây | 0,56% chiều cao |
| `7703457196239872678` (19 phút, g1 → g4) | 0,165 → 0,012 USD | 19,6 phút → 48 giây | 0,14% chiều cao |

- **Tìm phụ đề:** từ 950–1.150 giây xuống 7–23 giây, từ khoảng 2.300–2.400 lần gọi model xuống 18–76.
- **Đọc câu bổ sung:** chậm hơn, mất 41–42 giây so với 13–27 giây. Có hai nguyên nhân:
  - Mốc phụ đề giờ thô hơn (một frame mỗi 32–64 giây ở lưới dừng), nên số khoảng phải đọc tăng nhẹ: 44 so với 28 lần gọi, và 55 so với 39.
  - Cả hai lần đều gặp lỗi tạm thời `invalid timestamped readings` và Temporal phải chạy lại activity.
- **Câu bổ sung từ phụ đề:** gần với g1.
  - Chỉ chắc chắn mất 1 câu thoại, "闭嘴！ 닥쳐!". Có thể mất thêm "哈哈哈" (tiếng cười).
  - Bản mới thêm 13 câu g1 không có. Khoảng một nửa là thoại thật, còn lại là tên nhân vật, chú thích địa điểm và biển hiệu.
  - ASR được chạy lại ở generation mới nên transcript cũng khác đi một chút. Nhiều câu "chỉ g1 có" thật ra là câu ASR mới đã tự nghe được.

Số liệu và trang so sánh ở artifact https://claude.ai/artifact/QbBesKqLpqQDuXkZ5ZrC1o. Dữ liệu nằm trong `data/ocr-bench/sampling-v1/`.

## Các bước đã làm, theo thứ tự

1. **Tìm bước tốn tiền nhất.** Dựa trên `localization.llm_calls`:
   - `detect_subtitles` tốn khoảng 7,1e-05 USD mỗi frame, và mỗi video có hàng nghìn frame.
   - `complete_transcript` tốn khoảng 1,5e-04 USD mỗi lần đọc, nhưng mỗi video chỉ đọc vài chục lần.

   Phần tiền nằm gần hết ở bước tìm phụ đề.
2. **Thử chạy model OCR local** (bench `eval/local_ocr_dataset_bench.py`, dữ liệu `data/ocr-bench/dataset-v1`, 10 video, đúng prompt và 2 fps như production):
   - DeepSeek-OCR (mlx) trả lời rỗng với prompt production trên 229 frame.
   - Nhánh "grounding" (prompt riêng cho DeepSeek-OCR) bị dừng, vì nó không thể làm baseline cho việc tối ưu.
   - Qwen3-VL-8B bị dừng giữa chừng khi hướng đi đổi.
   - Đã xóa cả hai model để lấy lại dung lượng đĩa.
3. **Định nghĩa lại bài toán.** Box mờ luôn được vẽ suốt cả video, nên chỉ cần biết hàng chứa phụ đề. Không cần hỏi từng frame. Đo trên 44 bản render cho thấy:
   - Không thể dùng một dải cố định: video ngang có phụ đề ở 0,77–1,00 chiều cao, video dọc ở 0,47–0,53.
   - Lấy mẫu thưa vẫn giữ được vị trí box.
   - Rủi ro thật nằm ở `complete_transcript`, vì bước này dùng mốc phụ đề để chọn khoảng đọc.
4. **Bench mô phỏng không gọi model** (`eval/subtitle_sampling_gepa.py`):
   - Phát lại câu trả lời production đã lưu cho từng frame, tính tiền theo giá thật, đếm câu bị sót và đo độ lệch box.
   - Chia tập: 23 video học, 11 video chọn, 10 video kiểm tra.
   - Chấm "hiện tại", "làm tay" (một frame mỗi 4 giây và đọc mọi khoảng im lặng), "GEPA" và "làm tay + mẹo GEPA".
5. **GEPA tối ưu code chiến lược** (`optimize_anything`, model viết lại code là `claude -p`, 30 lần đề xuất, khoảng 63 phút). Trên 10 video kiểm tra, bản GEPA đạt 0,0077 USD mỗi video và sót 1/48 câu. Chiến lược này có hai phần:
   - **Tìm vùng phụ đề:** được giữ và đưa vào production. `detect_subtitles` hiện chạy chính cách này: bắt đầu từ lưới khoảng 8 frame, chia đôi cho tới khi có ít nhất 10 box và mép vùng lệch không quá 0,5% chiều cao so với một lưới thưa hơn. Gần hết khoản tiết kiệm đến từ phần này.
   - **Chọn khoảng đọc:** bị loại. Phần này gộp nhiều khoảng im lặng vào một lần đọc và bỏ những khoảng không có box ở gần. Xem phần quyết định.
6. **Sửa code production** theo hướng "làm tay + mẹo GEPA", rồi chạy thật trên workflow:
   - g2 / g3: tìm phụ đề mới, đọc mọi khoảng im lặng của cả video.
   - g3 / g4: tìm phụ đề mới, đọc câu theo cách cũ.

## Quyết định đã đổi

| Ban đầu | Đổi thành | Vì sao |
|---|---|---|
| Chạy model OCR local thay OpenRouter | Bỏ | DeepSeek-OCR trả lời rỗng với prompt production. Sau đó người dùng chỉ ra rằng chỉ cần biết hàng chứa phụ đề, không cần hỏi mọi frame |
| GEPA tối ưu prompt OCR | GEPA tối ưu code chọn frame và chọn khoảng đọc | Cái tốn tiền là số lần hỏi, không phải chất lượng prompt |
| Model viết lại code cho GEPA dùng OpenRouter (tốn tiền) | `claude -p`, theo hướng dẫn của chính GEPA | Người dùng không muốn tốn tiền API cho việc này |
| Dùng nguyên bản GEPA | Chỉ lấy cách tìm box của GEPA ("làm tay + mẹo GEPA") | GEPA gộp nhiều khoảng im lặng vào một lần đọc và canh để 8 frame rơi đúng vào những chỗ mô phỏng dùng để chấm. Đó là học theo cách chấm. Nó còn sót 8/168 câu trên tập học (bản làm tay sót 4), mà chỉ rẻ hơn bản làm tay 0,009 USD mỗi video |
| Đọc mọi khoảng im lặng của cả video | Trả `complete_transcript` về cách cũ | Chạy thật thì một video thêm 37 câu g1 không có, phần lớn là rác: tên nhân vật, địa điểm, biển hiệu, dòng cảnh báo, chữ đọc sai. Bước đọc cũng đắt và chậm hơn (132 so với 28 lần gọi). Bench không đo được lỗi thêm nhầm câu này |
| Chạy hai bước OCR bằng script ngoài workflow | Tăng generation và chạy thật trên workflow, dừng sau OCR | Người dùng yêu cầu chạy thật. Bản chạy ngoài workflow bị bỏ và script đã xóa |
| `READ_CONCURRENCY` 4 → 12 | Giữ 4 | Đổi lên 12 chỉ có ý nghĩa khi đọc cả video. Khi đã trả bước đọc câu về cách cũ thì bỏ luôn thay đổi này |
| Thêm `duration_ms` vào `SubtitleDocument` để đọc cả video | Giữ trường này dù bước đọc đã trả về | Tài liệu phụ đề của g2, g3, g4 đã lưu có trường này. Contract đặt `extra=forbid`, nên xóa trường sẽ làm các bước đọc lại tài liệu đó bị lỗi |

## Sự cố trong lúc chạy thật

- **Chạy bộ test song song với hai workflow.** Bước tìm phụ đề của `7703457196239872678` g2 chạy đúng lúc test đang chiếm CPU. Số đo thời gian của lần đó bị bỏ, và video được chạy lại ở g3 khi máy rảnh.
- **Worker trên host còn chạy code cũ.**
  - `reup-voice-worker` được khởi động trước commit watermark (2026-11-15 22:31), nên không đọc được `collection.watermarks` trong input và làm `transcribe` lỗi. Workflow g2 của `7703457196239872678` vì vậy lỗi hẳn.
  - `reup-ingest-worker`, nơi chạy `detect_watermarks`, không biết trường `duration_ms` mới. Lỗi này do chính thay đổi contract trong phiên.

  Đã khởi động lại `reup-voice-worker`, `reup-ingest-worker`, `reup-review-worker` và container `review-bridge`.
- **Theo dõi không báo lỗi kịp.** Script theo dõi đầu tiên chỉ thoát khi mọi workflow xong, nên một workflow lỗi nằm im gần 10 phút mà không ai biết, cho tới khi người dùng hỏi. Script đã được viết lại để thoát ngay ở sự kiện đầu tiên: activity lỗi (kể cả lỗi đang được thử lại), workflow đóng, hoặc OCR xong (dừng workflow luôn).
- **Bộ lọc quảng cáo.** `classify_video` xếp `7702770865292758299` vào loại quảng cáo ở cả g2 lẫn g3, còn g1 thì không. Đã bấm "Chạy tiếp" (`bypass_skip`) qua API dashboard. Lượt chạy tiếp chạy lại cả `detect_subtitles`. Script so sánh lấy mỗi bước từ lượt chạy đã làm bước đó, nên không cộng trùng tiền.

## Sự thật khó nghe

- **Bench mô phỏng nói "rẻ hơn và không sót câu nào", nhưng không nói gì về câu thêm nhầm.** Chính loại lỗi này làm hỏng hướng đọc cả video. Bench chỉ đo được những gì production đã từng làm. Mọi thay đổi làm model đọc ở chỗ production chưa từng đọc đều phải chạy thật trước khi tin.
- **Thay đổi contract phải deploy cho mọi tiến trình đọc contract đó,** kể cả worker pm2 trên host chứ không chỉ container. Journal phase 09 đã ghi đúng bài học này mà lần này vẫn mắc lại.

## Việc còn mở

- **Generation hiện tại của hai video đã đổi.** `7702770865292758299` giờ đang ở g3, `7703457196239872678` ở g4. Các generation này đã bị dừng sau OCR và không có bản render. Bản render g1 đã duyệt vẫn còn trong `final_renders`, nhưng dashboard tính trạng thái theo generation hiện tại.
- **Bộ test đầy đủ không xanh, nhưng lỗi có từ trước thay đổi này.**
  - 45 test trong `tests/activities/test_layout_captions.py` lỗi vì Pillow trên máy không có raqm.
  - 59 test ở các thư mục khác cũng lỗi y như vậy trên code gốc (`git stash`). Trong đó có `test_agent_wiring`, `test_openai_compat`, `test_contract_hardening`, và hai test ở `test_prep_stage`: `test_the_workflow_names_only_activities_that_are_registered`, `test_the_real_clips_final_cover_hugs_the_text`.
  - Khi chạy cả bộ, `test_resume_tts` vượt giới hạn 60 giây của pytest-timeout. Chạy riêng thì qua cả 7 test.
  - Các test của `detect_subtitles` mới đều qua.
- **Mốc phụ đề thô làm mất ít câu thoại.** Mới chắc chắn mất 1 câu trên 2 video. Cần theo dõi thêm trên các video mới.
- **Dữ liệu bench còn trên đĩa:** `data/ocr-bench/dataset-v1` (367 MB) và `gepa-v1` (9 MB) của hướng OCR local đã bỏ.
