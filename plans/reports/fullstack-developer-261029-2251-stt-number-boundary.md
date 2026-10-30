# STT number boundary (faster-whisper "4." "6")

## Nguyên nhân thật

Ranh giới giữa segment 4 và 5 không phải do segment của provider mà do chính `regroup`
cắt. Khi có word timestamps, `regroup` dựng lại segment từ words và bỏ qua segment của
provider. faster-whisper gắn dấu vào chữ số đứng trước, nên trả `"4."` (34120–34360) rồi
`"6"` (34440–34620), cách nhau 80 ms. `_numbers` chỉ nối những word toàn chữ số hoặc dấu
đứng riêng (dạng whisperx), nên không nhận ra `"4."`. Sau đó `_best_cut` xếp hạng `"4."` là
cuối câu (vì `.` nằm trong `SENTENCE_ENDS`) và cắt ngay đó, khi cụm 26980–48260 dài quá 60 ký tự.

## Thay đổi

- `services/localization/src/localization/activities/transcribe.py`: `_numbers` và hàm mới
  `_number_end` nối chuỗi dài nhất các word là mảnh số (`[0-9.,]+`, word đầu bắt đầu bằng
  chữ số), nói liền nhau (`NUMBER_GAP_MS` = 100 ms, giữ nguyên vì khoảng 80 ms thực tế nằm
  trong ngưỡng), sao cho khi nối lại thành một số hợp lệ
  `[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?` và không dừng giữa hai chữ số nói liền nhau. Cách này
  xử lý cả `"4" "." "6"` của whisperx lẫn `"4." "6"` và `"1," "000"` của faster-whisper.
  `_holds` có sẵn giữ số và lượng từ/đơn vị theo sau (`"安"`) ở cùng segment. Bỏ
  `_digit_run` và `_DIGITS`, vì cả hai không còn được dùng.
- Không đổi: input whisperx (toàn bộ test cũ vẫn xanh), nhánh không có word timestamps,
  text không phải số (`"15" "."`, dấu phẩy mệnh đề `"3," "5"`, `"4."` có khoảng nghỉ phía sau),
  và `words` lưu lại vẫn là words của provider.
- Hình dạng dữ liệu: `providers/openai_stt.py` đã đọc `words` ở cấp trên cùng, nếu không có
  thì gom `segments[].words`, nên words của cả hai provider đều tới `regroup` dưới dạng
  `SttWord`. Không cần sửa provider.

## Test (TDD, `tests/workflows/test_prep_stage.py`)

- `test_a_number_faster_whisper_ends_a_word_inside_is_one_number_in_one_segment`: words thật
  của 7616569292510948709 (26980–37920). Test đỏ trước khi sửa, xanh sau khi sửa.
- `test_faster_whisper_number_fragments_join_only_into_a_real_number`: `"1," "000"` được nối,
  còn `"3," "5"` và `"4." <pause> "6"` giữ nguyên như trước. Test đỏ trước khi sửa, xanh sau khi sửa.
- `uv run pytest -q tests/workflows/test_prep_stage.py tests/activities`: 363 passed.
  `ruff check` và `ruff format --check` đều sạch.

## Kiểm tra trên dữ liệu thật

Nạp mã nguồn mới vào `docker compose exec -T worker-agents python` (không sửa image), rồi
chạy `regroup` trên transcript đã lưu (words của provider):

7616569292510948709: 9 segment thành 8, words giữ nguyên.

- Trước, 4: `…效率特别高,小小一颗就有4.`
- Trước, 5: `6安纳铁,每天吃三颗,刚好满足一天所需,`
- Sau, 4 (26980–35100): `…效率特别高,小小一颗就有4.6安纳铁,`
- Sau, 5 (35240–44340): `每天吃三颗,刚好满足一天所需,相当于吃了半斤枸杞,…它一颗就能半斤搞定,` (59 ký tự,
  ≤ 60). Segment cũ 5 và 6 gộp lại vì cụm câu giờ được cắt ở chỗ khác.

7618513827751629177: 11 segment, không đổi.

## Sai khác so với yêu cầu

Yêu cầu nói "ưu tiên dời mảnh số sang segment sau". Vì ranh giới là của `regroup` chứ không
phải của provider, tôi không viết bước sửa ranh giới riêng. Thay vào đó, số được nối thành
một word từ đầu, và `_best_cut` (vốn không bao giờ cắt trong số hay giữa số và lượng từ) chọn
chỗ cắt tại dấu phẩy mệnh đề sau `铁,`. Kết quả là số nằm ở segment trước, cùng câu
`就有4.6安纳铁`. Nếu ép số sang segment sau thì sẽ tách `就有` khỏi tân ngữ của nó.

Status: DONE_WITH_CONCERNS
Summary: Số bị tách là do faster-whisper trả "4." thành một word, `_numbers` không nối được, và `_best_cut` coi "." là cuối câu. Giờ "4.6安纳铁," nằm trọn trong một segment, transcript của 7618513827751629177 không đổi, và 363 test xanh.
Concerns/Blockers: Không áp dụng quy tắc "ưu tiên segment sau" vì ranh giới do regroup tạo ra; số được giữ trong câu của nó ở segment trước. Transcript của 7616569292510948709 chỉ thay đổi khi chạy lại bước transcribe (generation mới).
