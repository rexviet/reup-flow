# TTS theo từng đoạn ngắn (Đợt 2, mục D)

Trạng thái: xong. Workflow không đổi. Tên activity, tham số, kết quả và hợp đồng `Clip` (key, `duration_ms`, cache) đều giữ nguyên.

## Đã sửa
- `services/localization/src/localization/activities/tts_pieces.py` (mới): `split_line` cắt dòng thành các đoạn, `join_pieces` ghép audio của các đoạn.
- `services/localization/src/localization/activities/synthesize.py`: thêm `Synthesizer._speak`. Hàm này gọi TTS lần lượt cho từng đoạn rồi ghép lại. Phần cache, kiểm tra `TextTooLong`, `before_put` và metadata vẫn tính trên cả dòng.
- `services/localization/tests/activities/test_tts_pieces.py` (mới, 25 test) và `tests/activities/test_synthesize.py` (thêm 7 test).

## Quy tắc cắt
1. Gộp khoảng trắng, sau đó cắt ở cuối câu: `. ! ? …` (và `。！？`) khi theo sau là khoảng trắng. Có hai trường hợp không cắt:
   - ký tự tiếp theo là chữ thường, ví dụ "vitamin C … đã có" hay "v.v. đều";
   - từ đứng trước dấu chấm là chữ viết tắt (2–3 chữ in hoa như "TP.", "PGS.", hoặc Dr/Mr/Mrs/Ms/St/ThS/Tp).
2. Mỗi câu là một đoạn. Câu dài hơn `MAX_PIECE_CHARS = 80` thì được cắt tiếp ở `, ; :` (khi theo sau là khoảng trắng). Các mệnh đề liền nhau được gộp tham lam thành đoạn dài tối đa 80 ký tự. Một mệnh đề tự nó đã dài hơn 80 ký tự thì giữ nguyên, vì không có chỗ cắt an toàn.
3. Chỉ cắt ở dấu câu có khoảng trắng phía sau. Vì vậy "4.6", "1,5", "10.000", "3:30", "bốn phẩy sáu" và tên riêng không bao giờ bị cắt. Khi không chắc thì không cắt: một đoạn hơi dài vẫn tốt hơn một cái tên bị đọc làm hai.

Lý do chọn 80 ký tự: con số này tương đương khoảng 5 giây tiếng Việt, chưa bằng một nửa dòng ngắn nhất từng bị đọc thiếu (khoảng 150 ký tự). Nó cũng đủ dài để phần lớn mệnh đề nằm trọn trong một đoạn, nên một dòng dài thường chỉ thành 2–3 đoạn chứ không phải mỗi dấu phẩy một đoạn.

## Khoảng nghỉ và cắt lặng
- Nghỉ sau câu là `SENTENCE_PAUSE_MS = 200` ms, sau mệnh đề là `CLAUSE_PAUSE_MS = 120` ms.
- Chỉ cắt lặng ở chỗ hai đoạn giáp nhau. Ngưỡng lặng là `SILENCE_THRESHOLD = 0.01` (khoảng -40 dBFS), và chừa lại `TRIM_MARGIN_MS = 30` ms để không mất phụ âm nhẹ hay tiếng thở. Đầu đoạn đầu và cuối đoạn cuối được giữ nguyên như provider trả về, nên dòng nhiều đoạn có hai mép giống hệt dòng một đoạn. Một đoạn im lặng hoàn toàn được giữ nguyên chứ không bị xoá mất.
- Trước khi ghép, từng đoạn được ffmpeg giải mã về mono, 16-bit, 24 kHz.
- `duration_ms` vẫn đo bằng ffprobe trên file WAV cuối cùng, giống như hiện nay. Độ dài đó đúng bằng tổng các đoạn (sau khi cắt lặng) cộng các khoảng nghỉ, và có test xác nhận.
- Dòng chỉ có một đoạn thì gửi một request, và bytes của provider được lưu nguyên vẹn, không qua ffmpeg.
- Lỗi được xử lý như cũ. Lỗi transient của một đoạn khiến cả clip thất bại và được retry. Lỗi permanent thì không retry. ffmpeg không đọc được một đoạn cũng là lỗi permanent. Khi lỗi thì không có gì được ghi vào cache. Heartbeat chạy nền như cũ.

## Dòng 4 của video 7616569292510948709 được cắt thế nào
Tôi không có toàn văn dòng 4 (không được đọc dữ liệu thật), nên phần đuôi dưới đây là phần minh hoạ. Dòng thử dài 142 ký tự:

| # | ký tự | nghỉ sau | đoạn |
|---|---|---|---|
| 1 | 16 | 200 ms | `sáu miligam sắt.` |
| 2 | 76 | 120 ms | `Mỗi ngày uống ba viên là vừa đủ, sáng một viên, trưa một viên, tối một viên,` |
| 3 | 48 | 0 | `uống sau bữa ăn để dạ dày không bị khó chịu nhé.` |

Câu đầu, đúng phần đã bị đọc mất, giờ thành một request riêng. Dòng 3 (115 ký tự) được cắt thành hai đoạn:
- `Dịu nhẹ, dễ hấp thu, không kích ứng, còn kết hợp thêm peptide hemoglobin,` (73 ký tự, nghỉ 120 ms);
- `anthocyanin và vitamin C … đã có bốn phẩy` (41 ký tự).

## Test
- `uv run pytest -q tests/activities/test_synthesize.py tests/activities/test_tts_pieces.py`: 81 pass. Trước khi sửa `synthesize.py`, 4 test mới đã fail đúng như dự kiến.
- `uv run pytest -q tests/activities tests/workflows/test_dubbing_stage.py`: 273 pass.
- `uv run pytest -q tests/architecture`: 31 pass.
- `ruff check` và `ruff format --check` trên 4 file đã sửa: sạch.
- Kiểm tra đột biến thủ công: đã phá từng ràng buộc mới một lần (quy tắc chữ thường, chữ viết tắt, gộp mệnh đề, cắt câu dài, cắt lặng, khoảng nghỉ, giữ mép đầu, đoạn im lặng, chạy tuần tự, đường một đoạn, fallback dòng rỗng). Mỗi lần đều có ít nhất một test chuyển đỏ. Hai test ban đầu không bắt được đột biến (đoạn im lặng nằm ở cuối, và dòng ngắn đi qua join vẫn ra cùng bytes) nên tôi đã sửa lại cho chặt hơn.

## Lưu ý
- Độ trễ tăng thêm: mỗi đoạn là một round-trip tới Colab, và các đoạn chạy tuần tự. Một dòng 2–3 đoạn sẽ tốn thêm 1–2 lần overhead request (thời gian khởi động model và mạng, thường cỡ vài trăm ms tới khoảng 1 s mỗi lần, chưa đo trên server thật). Phần sinh audio thì gần như không đổi vì tổng số ký tự không đổi. Mỗi lần ghép tốn thêm vài chục ms cho ffmpeg trên mỗi đoạn.
- Clip nhiều đoạn dài hơn một chút so với đọc liền: thêm 120–200 ms ở mỗi chỗ nối và tối đa 60 ms phần chừa lại khi cắt lặng. Lượng này đi vào phần fit/stretch như mọi độ dài khác.
- Clip nhiều đoạn được chuẩn hoá về 24 kHz mono. Nếu provider trả về tần số cao hơn thì clip ghép bị hạ tần số. Bản mix cuối là 44.1 kHz.
- Cache cũ vẫn hợp lệ vì hash không đổi. Các dòng dài đã có trong cache sẽ không được đọc lại theo đoạn cho tới khi cache đó bị xoá.
- Chưa chắc 80 ký tự đã đủ ngắn: đoạn 73 ký tự của dòng 3 vẫn chứa đúng cụm từng bị đọc mất. Cần nghe thử trên một video thật để biết. Nếu vẫn thiếu chữ thì có thể giảm `MAX_PIECE_CHARS`.
- Ngữ điệu có thể bị ngắt giữa các đoạn vì mỗi request được đọc độc lập.
