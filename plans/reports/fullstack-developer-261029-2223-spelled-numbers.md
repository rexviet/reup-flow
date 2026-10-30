# Kiểm tra số đồng nhất giữa review và slot shortener

## Kết quả

Review (`line_findings`) và slot shortener giờ dùng chung một hàm kiểm tra số. Dòng
"Ba mươi tuổi mà trông như hai mươi" không còn bị đánh lỗi "the source has 20", và
"bốn phẩy sáu miligam" không còn bị đánh lỗi "the source has 6".

## Hàm dùng chung nằm ở đâu

Hàm nằm trong module mới `services/localization/src/localization/text/vi_numbers.py`, với ba
hàm public:

- `source_numbers(text)` trả về các số của câu nguồn, mỗi số một lần, viết như câu nguồn viết ("4.6", "1,500", "3万").
- `carries_number(number, line)` cho biết dòng tiếng Việt có mang số đó hay không.
- `missing_numbers(source, line)` trả về các số của nguồn mà dòng không mang. Cả hai bên gọi hàm này.

Tôi chọn `text/` thay vì để hàm public trong `review_checks`, vì `text/` đã chứa các helper
thuần về văn bản tiếng Việt (`vi_syllables`, `glyphs`), và cách này cắt hẳn việc một agent
import hàm private của agent khác. `_digits_kept` đã bị xoá. `slot_shortener` không còn
import gì từ `review_checks`.

## Các dạng số được chấp nhận

Hàm so sánh giá trị, không so chuỗi con. Vì vậy "bảy mươi lăm" không mang 70, "20" không mang 2
và "một trăm hai mươi" không mang 20.

- Chữ số giống nguồn, kể cả "4,6", "4.6", "1.500", "1,500" và "iPhone15".
- Số nguyên viết bằng chữ: các chữ số từ không đến chín, "mười", "mươi", "mốt", "tư" và "lăm" (chỉ sau mười/mươi), "linh/lẻ", "trăm", "nghìn/ngàn", "vạn", "triệu", "tỷ/tỉ". Có cả dạng "hai nghìn không trăm hai mươi bốn" và dạng của num2words là "hai nghìn lẻ hai mươi bốn".
- Số thập phân với "phẩy". Phần sau dấu phẩy có thể đọc từng chữ số ("ba phẩy một bốn") hoặc đọc thành số ("ba phẩy mười bốn").
- "rưỡi" ở dạng đơn giản: "hai rưỡi" là 2,5, "một trăm rưỡi" là 150, "một nghìn rưỡi" là 1500.
- Ở nguồn, "4.6" được đọc là một số, không bị tách thành 4 và 6.
- Phần trăm và đơn vị giữ như trước: "%" và đơn vị không bắt buộc, nên với "50%" thì "năm mươi" là đủ.

Hai điểm này cần thêm để không bị thoái lui, vì cách so chuỗi con cũ vô tình chấp nhận chúng:

- Số đứng trước một từ hàng (nghìn, triệu, ...) vẫn được tính. Ví dụ "30元" thành "ba mươi nghìn". Test cũ `test_a_number_of_the_source_must_reach_the_line_as_digits_or_as_words` đòi hỏi điều này.
- Số nguồn có 万 hoặc 亿 phía sau cũng được nhận ở giá trị đầy đủ, nên "3万" khớp với "ba mươi nghìn" hoặc "3 vạn".

"năm" sau "mười/mươi" có chủ ý không được tính là chữ số 5, vì "hai mươi năm" là hai mươi năm (thời gian). Muốn nói 25 thì phải viết "hai mươi lăm".

## Test

- `tests/text/test_vi_numbers.py` là file mới, có 1165 case. Trong đó có 8 case về số của nguồn, 41 case dòng mang số, 12 case dòng làm rơi số hoặc sai số, và 1107 case round-trip với num2words (0 đến 1099 cộng một số mốc lớn).
- `tests/agents/test_review_checks.py` thêm 7 case: 4 câu thật của run lỗi phải pass; thiếu 70, sai thành 75 và thiếu 4.6 phải fail và nêu đúng số.
- `tests/agents/test_slot_shortener.py` thêm 11 cặp, khẳng định verdict của `slot_shortener.problem` trùng với `review_checks.line_findings`.
- Trước khi sửa, 4 test của review thất bại (red). Test đồng nhất pass ngay từ đầu, vì hai bên đã dùng chung một helper sai. Test này là chốt chặn cho sau này, không phải bằng chứng red.
- Lệnh `uv run pytest -q tests/agents tests/activities tests/workflows tests/replay` cho 1051 passed và 2 deselected. Lệnh `tests/text` cho 1256 passed.
- `ruff check` và `ruff format --check` trên 6 file đã sửa đều sạch.

## Lo ngại

- Kiểm tra giờ chặt hơn ở một điểm: cách so chuỗi con cũ chấp nhận "20" cho số 2 và "bảy mươi lăm" cho 70, còn bây giờ thì không. Như vậy có thể xuất hiện finding mới mà trước đây bị bỏ sót.
- Không ảnh hưởng replay: kiểm tra chạy trong activity, và `tests/replay` vẫn pass.
- Slot shortener vẫn chỉ đòi giữ các số viết bằng chữ số trong dòng gốc. Nếu dòng gốc tiếng Việt viết số bằng chữ ("bảy mươi") mà bản rút gọn bỏ mất, việc đó vẫn không bị bắt, giống như trước. Tôi chưa mở rộng phần này vì nó nằm ngoài yêu cầu.
- Chưa hỗ trợ: "chục", "chấm" dùng làm dấu thập phân, số viết có dấu cách nhóm ("10 000"), và "rưỡi" khi có đơn vị chen giữa ("hai tiếng rưỡi"). Cách cũ ghép cả "20 30" thành 2030, nên bỏ việc ghép theo dấu cách là có chủ ý.
- "một" đứng như mạo từ vẫn được tính là số 1. Điều này giống như trước và vô hại.
- Không có mutation run, theo quy ước của plan là để dồn mutation run đến cuối.
