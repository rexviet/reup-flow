# Number idioms in the Vietnamese number check

## Result

Câu thật `30岁更像20岁` -> "Ba mươi tuổi mà trông như đôi mươi" giờ qua `line_findings`,
không còn finding "the source has 20, which the line does not carry". "Ngũ" vẫn không
được tính là 5.

## Files

- `services/localization/src/localization/text/vi_numbers.py`: `_tens` đọc thêm
  `["đôi", "mươi"]` = 20 và `<chữ số 1..9> chục` = chữ số x 10; thêm "đôi", "chục" vào
  `_WORDS`; docstring module ghi rõ cái gì được đọc, cái gì không.
- `services/localization/tests/text/test_vi_numbers.py`: hai test parametrize mới
  (idiom được chấp nhận; cặp, số lượng mơ hồ, "tá", "Ngũ" không được chấp nhận).
- `services/localization/tests/agents/test_review_checks.py`: thêm một dòng vào
  `test_a_number_spelled_in_vietnamese_words_is_carried` với đúng cặp câu thật. Không đụng
  `residual_findings` hay `review_checks.py`.

## Quyết định từng cách viết

- "đôi mươi" = 20: CHẤP NHẬN. Đây là thành ngữ cố định chỉ tuổi hai mươi ("tuổi đôi
  mươi"). Chỉ nhận đúng hai từ đó; "đôi mươi mốt" không thành 21, và "đôi mươi" không
  mang số 2.
- "đôi" một mình = 2: TỪ CHỐI. "một đôi giày" là một cặp, "đôi khi", "đôi chút" không phải
  số.
- "<chữ số> chục" = chữ số x 10: CHẤP NHẬN, chỉ khi đứng trước là chữ số 1..9 ("một chục"
  10, "hai chục" 20, "năm chục" 50). Vì đi qua `_tens`, nó cũng ghép được với hàng nghìn
  ("ba chục nghìn" = 30000, giữ count 30 như "ba mươi nghìn") và hàng trăm ("một trăm hai
  chục" = 120).
- "chục" một mình, "vài chục", "mấy chục", "hàng chục", "dăm chục", "mười chục": TỪ CHỐI.
  Đều là số lượng mơ hồ hoặc không phải cách nói chuẩn; các từ đứng trước không nằm trong
  `_UNITS` nên tự động không đọc được. "không chục" bị chặn bằng điều kiện chữ số khác 0.
- "tá" = 12: TỪ CHỐI. Không đủ rõ nghĩa: "tá" là âm tiết của nhiều từ khác ("đại tá",
  "thiếu tá", "phụ tá", "tá điền", "tá hỏa") nên "ba tá điền" sẽ bị đọc thành 36; và "một
  tá" thường được dùng với nghĩa "rất nhiều" giống "a dozen" trong tiếng Anh.
- "Ngũ" = 5: giữ nguyên là finding (có test khóa lại "Ngũ", "Anh Ngũ").

## Kiểm tra

- Red trước: 10 test idiom và test `line_findings` với cặp thật đều fail đúng lý do (chưa
  đọc "đôi mươi"/"chục").
- `uv run pytest -q tests/text tests/agents`: 1565 passed, 3 deselected.
- `ruff check` và `ruff format --check` trên ba file: sạch.
- Service không cấu hình mypy/pyright nên không có bước typecheck riêng.

## Ghi chú

- Ở miền Nam, "một chục" khi mua trái cây có thể là 12 hoặc 14 quả. Trong lời dịch thông
  thường "một chục" vẫn là 10, và rủi ro chỉ là chấp nhận nhầm một câu mang đúng ý, nên
  vẫn nhận.
- "hai chục rưỡi" (nói tắt 25) không được đọc thành 25 (cho ra 20.5, vô hại). Không thêm vì
  hiếm và ngoài phạm vi.

Status: DONE
Summary: "đôi mươi" (20) và "<chữ số 1..9> chục" giờ được tính là số; "tá", "đôi" một mình, các cách nói chục mơ hồ và "Ngũ" vẫn bị từ chối. Câu thật đã qua line_findings, toàn bộ tests/text và tests/agents xanh.
Concerns/Blockers: Không có.
