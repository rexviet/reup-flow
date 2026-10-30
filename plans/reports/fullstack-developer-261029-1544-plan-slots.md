# Bước `plan_slots`: dò giọng nói, gộp slot, chia chữ vào slot bằng code

Ngày: 2026-10-29. Thuộc kế hoạch `plans/261029-1544-vbee-dubbing-pipeline/plan.md`, phần activity `plan_slots`.

## Kết quả

- Activity mới nằm ở `services/localization/src/localization/activities/plan_slots.py`. Activity chưa được đăng ký vào worker nào và không có workflow nào gọi nó; việc nối vào workflow để cho bước sau.
- Chạy trên dữ liệu thật của 7616 và 7618, bước này cho ra 13 và 15 slot. Không slot nào kết thúc giữa cụm từ và không cần gộp slot nào. Ranh giới thời gian trùng khớp với spike.
- Có 33 test trong `tests/activities/test_plan_slots.py`, tất cả đều đạt. Bộ `tests/activities` đạt 288/288. `ruff check` và `ruff format --check` đều sạch.
- Contract `contracts/vbee_flow.py` giữ nguyên. Không có Vbee hay LLM nào được gọi, và không có commit nào.

## Thuật toán và tham số

**1. Dò giọng nói.**
- Chạy `ffmpeg silencedetect` với `noise=-30dB` và `d=0.15`. Lệnh ffmpeg đi qua `run_ffmpeg`, độ dài file lấy bằng `probe_duration_ms`. Phần còn lại sau khi bỏ các khoảng lặng là các đoạn nói.
- Lý do chọn -30 dB / 0,15 s: mức này bắt được cả những chỗ ngắt hơi giữa các cụm từ, khoảng 1,5–3 s một lần trên 7616. Spike đã thử mức chặt hơn (-35 dB / 0,25 s) và không tìm thấy khoảng lặng nào trong 27 s của 7616. Điểm cắt dư không gây hại, vì bước gộp slot chỉ chọn một phần trong số đó.
- Khi khoảng lặng kéo tới hết file, nó được tính đến cuối file. Mốc thời gian nằm ngoài [0, độ dài] bị kẹp lại cho vừa.

**2. Phát hiện video không có lời.** `has_speech=False` và không tạo slot nào khi rơi vào một trong ba trường hợp sau:
- tổng thời gian nói ít hơn `MIN_SPEECH_MS` = 2000 ms (lý do ở phần sau);
- transcript không có chữ nào;
- không đoạn nói nào nằm trong khoảng thời gian của một utterance.

Lý do được ghi vào `notes`.

**3. Bỏ các đoạn nói nằm ngoài mọi utterance.** Tôi chọn bỏ chúng trước khi gộp slot, thay vì gộp vào slot bên cạnh. Ở những chỗ đó Vbee không nghe ra chữ nào, ví dụ tiếng ho hoặc một nốt hát. Nếu giữ lại thì slot sẽ hoặc câm, hoặc bị kéo dài qua quãng không có ai nói.

**4. Gộp các đoạn nói thành slot.** Tôi chuyển quy hoạch động của spike sang và dọn lại cho gọn:
- Mỗi slot bắt đầu từ chữ đầu tiên của nó. Slot kết thúc ở điểm bắt đầu của slot sau, nhưng không quá `PAUSE_SLACK_MS` = 1000 ms sau chữ cuối của nó. Spike thì cho slot nuốt trọn khoảng lặng phía sau; với một quãng lặng 5 s, slot sẽ kéo dài thêm 5 s qua cảnh không ai nói.
- Chi phí của mỗi slot là `((độ dài − 6000) / 3000)²`. Nếu slot nằm ngoài khoảng 3–10 s thì cộng thêm `10 + (phần vượt / 500)²`. Nếu slot kết thúc ở một khoảng lặng dài thì trừ `1,5 × min(khoảng lặng, 600 ms) / 600`: khoảng lặng từ 600 ms trở lên là cuối câu, không phải chỗ lấy hơi.
- Chương trình không xét slot dài hơn 60 s, trừ khi không còn cách nào khác. Nhờ vậy video dài vẫn chạy nhanh.
- Các giới hạn này là giới hạn mềm. Một quãng nói liền hơn 10 s mà không có chỗ ngắt nào vẫn thành một slot.

**5. Chia chữ vào slot.**
- **Tách phần chồng lấn giữa các utterance.** Khi thời gian của hai utterance chồng lên nhau, điểm chuyển giao là cuối khoảng lặng dài nhất nằm trong phần chồng lấn; nếu không có khoảng lặng nào thì lấy điểm giữa. Ví dụ ở 7616: utterance 1 kết thúc lúc 23,06 s, utterance 2 bắt đầu lúc 22,38 s, và trong khoảng đó có quãng lặng 22,39–22,88 s, nên điểm chuyển giao là 22,876 s.
- **Đặt từng từ vào slot.** Mỗi từ được đặt theo tỷ lệ âm tiết của nó trong utterance, trải đều trên thời gian có tiếng nói của utterance đó. Số âm tiết đếm bằng `count_syllables`, nên "146" tính là 6 âm tiết, không phải 1. Nếu đoạn vocals của một utterance hoàn toàn lặng, cả utterance được gán cho slot gần nó nhất về thời gian.
- **Dời điểm cắt về dấu câu.** Mỗi điểm cắt giữa hai slot được dời tới chỗ cắt hợp lệ gần nhất, trong phạm vi `MAX_SHIFT_WORDS` = 8 từ (khoảng 1 s lời nói). Nếu hai chỗ cách đều thì chọn chỗ sớm hơn. Điểm cắt mới luôn nằm sau điểm cắt trước đó và trước từ cuối cùng, nên slot nào cũng có ít nhất một từ.
- **Những chỗ được phép cắt:**
  - ranh giới giữa hai utterance;
  - trong một utterance có dấu câu ở giữa: chỉ sau từ kết thúc bằng `.,!?;:…` (bỏ qua dấu ngoặc và ngoặc kép đóng ở cuối từ);
  - trong một utterance không có dấu câu nào ở giữa câu: mọi ranh giới giữa hai từ (dấu chấm cuối câu không tính là dấu câu ở giữa);
  - không bao giờ cắt ngay sau một con số khi từ tiếp theo là số ("4, 6") hoặc là đơn vị hay từ chỉ loại ("4,6 mg", "146 loại").
- **Khi không có dấu câu nào trong phạm vi 8 từ,** hai slot hai bên được gộp làm một, và `notes` ghi lại việc gộp. Một câu dài thì bước rút gọn còn xử lý được; còn một câu kết thúc bằng từ treo thì TTS sẽ đọc ra lỗi.
- Kết quả trả về là `SlotPlan` với id chạy từ 0 đến n−1, các slot không chồng lên nhau.

**6. Activity.** `PlanSlotsActivity(store).plan_slots(spec, vocals_key, transcript) -> SlotPlan`, đăng ký với `@activity.defn(name="plan_slots")`. Activity làm các việc sau:
- tải vocals vào một `TemporaryDirectory` (thư mục này tự xóa khi xong việc);
- chạy ffmpeg trong `run_blocking`, nên activity gửi heartbeat trong lúc ffmpeg chạy;
- chuyển lỗi ffmpeg thành `ApplicationError` qua `provider_errors()`.

Nếu vocals không còn trong store, activity ném `ArtifactMissingError` loại non-retryable.

## Ngưỡng "không có lời" và bằng chứng

Tôi đo vocals thật lấy từ MinIO (`work/<id>/r0/vocals.wav`), với -30 dB / 0,15 s:

| Video | Nhãn | Dài (s) | Thời gian có tiếng (s) | Âm lượng max (dB) | `has_speech` |
|---|---|---|---|---|---|
| 7518248980598639930 | transcript rỗng | 22,7 | 0,00 | -37,4 | False |
| 7526777921518062905 | transcript rỗng | 18,1 | 0,00 | -44,8 | False |
| 7574654380532917477 | transcript rỗng | 11,7 | 0,00 | -47,3 | False |
| 7609902814123485361 | transcript rỗng | 16,2 | 0,00 | -48,9 | False |
| 7552039043057356090 | nghi là hát ("Thank / you"), chưa có nhãn | 20,5 | 1,35 (hai đoạn 0,69 s và 0,67 s, ở 16,2 s và 19,8 s) | -11,6 | False |
| 7565005737722907953 | hát | 28,0 | 26,68 | -3,5 | **True** |
| 7575765838708520689 | nói | 10,9 | 10,19 | -1,6 | True |
| 7665948504426075877 | nói | 23,0 | 21,57 | -1,2 | True |
| 7616569292510948709 | nói | 66,7 | 57,06 | -4,4 | True |
| 7618513827751629177 | nói | 83,8 | 73,00 | -2,1 | True |

Không lấy được 7664, vì key `work/7664/r0/vocals.wav` không tồn tại trong MinIO.

**Vì sao chọn 2000 ms:**
- Bốn video có transcript rỗng đều đo được đúng 0 s, nên mọi ngưỡng lớn hơn 0 đều loại được chúng.
- Video nói ngắn nhất có 10,2 s tiếng nói, nên ngưỡng 2 s cách xa nó.
- 2 s ít hơn một slot tối thiểu (3 s). Với hai từ rời rạc thì không có câu nào đáng để lồng tiếng.

7552 rơi vào nhóm "không có lời" với 1,35 s. Video này chưa có nhãn chắc chắn. Nếu đó thật sự là câu "thank you" nói ra, thì việc bỏ qua nó cũng chấp nhận được.

**Nói thẳng:** ngưỡng này không nhận ra được bài hát. 7565 có 95% thời gian có tiếng và chỉ ra một slot dài 26,7 s, vì người hát không có chỗ ngắt nào dài từ 0,15 s trở lên. Muốn nhận ra hát cần tín hiệu khác, chẳng hạn cao độ hoặc mô hình phân loại. Tôi không cố chỉnh ngưỡng để loại 7565 ở đây.

## Kết quả thật

Chạy offline:
- vocals lấy từ MinIO (qua `docker compose exec -T worker-media`);
- utterance của 7616 lấy từ `vbee/responses/zh_full-async-final.json`, của 7618 lấy từ `vbee-dub/7618513827751629177/stt-final.json`;
- script chạy là `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/07f62d39-b5c7-4084-a633-0d769a53fb9d/scratchpad/plan-slots/scripts/run_real.py`.

Cả hai video đều không cần gộp slot nào (notes trống), và không có slot nào kết thúc mà không có dấu câu.

### 7616569292510948709 (66,7 s, 31 đoạn nói, 13 slot)

| Slot | Thời gian (s) | Dài (s) | Chữ |
|---|---|---|---|
| 0 | 0,00–5,35 | 5,35 | Ba mươi tuổi trông như hai mươi tuổi, sức sống hướng thượng, ánh sáng tự túc của mình thật sự không bao giờ tắt. |
| 1 | 5,35–10,50 | 5,16 | Ăn sạch sẽ, tập thể dục đều đặn, nghỉ ngơi đúng giờ, và một điều tôi không bao giờ bỏ qua là bổ sung thích hợp. |
| 2 | 10,50–14,81 | 4,31 | Đặc biệt là cứ một trăm phụ nữ thì bảy mươi người thiếu sắt, nên càng sớm càng tốt. Bổ sung sắt, |
| 3 | 14,81–18,88 | 4,06 | tôi chỉ xem bốn điểm: nguồn sắt, hàm lượng, cảm giác khi uống, và tính an toàn. |
| 4 | 18,88–22,88 | 4,00 | So sánh vô số loại, tôi luôn mua lại là viên ngậm bổ sung sắt của Five Girls Doctor. |
| 5 | 22,88–27,26 | 4,39 | Nó khác với các loại sắt thông thường trên thị trường, sử dụng sắt hữu cơ chất lượng cao cấp năm, |
| 6 | 27,26–30,97 | 3,71 | dịu nhẹ, dễ hấp thụ, không gây kích ứng, còn kết hợp với huyết bạch đa peptide, |
| 7 | 30,97–38,29 | 7,32 | anthocyanin và nồng độ cao vitamin C, hiệu suất đặc biệt cao, chỉ một viên nhỏ đã có 4,6 mg sắt, ăn ba viên mỗi ngày vừa đủ nhu cầu một ngày, |
| 8 | 38,29–44,70 | 6,41 | tương đương với nửa cân kỷ tử, một cân thịt bò, hai cân táo đỏ, chúng ta bình thường không ăn được nhiều như vậy, nó một viên có thể giúp bạn giải quyết. |
| 9 | 44,70–49,06 | 4,35 | Vị mật ong chua ngọt rất dễ duy trì, lượng calo tương đương với một quả táo, |
| 10 | 49,06–54,95 | 5,89 | như tôi là người thường xuyên trong giai đoạn quản lý cũng không cần lo lắng. An tâm hơn là có 146 loại báo cáo không chứa hormone, |
| 11 | 54,95–60,12 | 5,17 | bà bầu, kỳ kinh nguyệt, cho con bú đều có thể ăn thoải mái. Phụ nữ ăn sắt thật sự phải sớm. |
| 12 | 60,12–66,69 | 6,58 | Hiện tại thương hiệu còn có hoạt động, trước đây một chai giá bây giờ nhận được ba lon lớn. Yêu bản thân, bắt đầu từ mỗi ngày ba viên kẹo bổ sung sắt. |

Điểm cắt bị dời tối đa 3 từ, trung bình 0,75 từ. 6 trong 12 điểm cắt không phải dời. "4,6 mg" và "146 loại" đều nằm gọn trong một slot. Utterance 1 kết thúc ở slot 4 ("…Doctor."), và slot 5 bắt đầu lúc 22,876 s.

### 7618513827751629177 (83,8 s, 41 đoạn nói, 15 slot)

| Slot | Thời gian (s) | Dài (s) | Chữ |
|---|---|---|---|
| 0 | 0,00–5,82 | 5,82 | Bây giờ là hai giờ sáng, cuối cùng tôi cũng đã chỉnh xong video sẽ đăng vào ngày mai. Những người làm tự do đều hiểu, ban ngày quay, |
| 1 | 5,82–10,39 | 4,57 | ban đêm chỉnh, thức khuya đã trở thành chuyện thường ngày. Mỗi ngày làm thêm giờ đã trở thành thói quen của tôi. |
| 2 | 10,39–15,49 | 5,11 | Sáng dậy đã bị cái này, tóc bị lên một lớp. Đầu lợn càng ngày càng xinh đẹp, một chỗ một chỗ. Lần trước đi làm tóc, |
| 3 | 15,49–20,56 | 5,07 | thợ làm tóc của tôi đã nói rồi. Cảm thấy mấy năm nay đường chân tóc của tôi cao lên rất nhiều. |
| 4 | 20,56–24,61 | 4,05 | Tôi tin rằng những người trẻ làm việc thêm giờ trong văn phòng, những học sinh lướt điện thoại vào ban đêm, |
| 5 | 24,61–29,52 | 4,91 | những người làm video giống tôi, đồng nghiệp chắc cũng giống tôi, những người thức khuya đều đang trả tiền cho chứng rụng tóc. |
| 6 | 29,52–36,65 | 7,13 | Làm việc thêm giờ lâu dài sẽ khiến nang tóc của chúng ta mất sức sống, dần trở nên cứng. Chúng ta thường nghĩ là do áp lực lớn, thực ra đều là do thức khuya đang âm thầm phá hủy sức khỏe nang tóc. |
| 7 | 36,65–41,50 | 4,85 | Đặc biệt là tôi là một blogger thường xuyên phải lên hình, đường chân tóc, trạng thái tóc trực tiếp ảnh hưởng đến cảm giác trên camera. |
| 8 | 41,50–48,04 | 6,54 | Rụng tóc thực sự sẽ làm tăng lo lắng, vì vậy sản phẩm chăm sóc tóc tôi chỉ xem độ chuyên nghiệp. |
| 9 | 48,04–54,38 | 6,34 | Thương hiệu Mandi này độ chuyên nghiệp không cần tôi nói nhiều. Chai dầu gội chống rụng tóc của nó, điều làm tôi thích nhất là chai đóng chân không, giữ được thành phần chính không bị oxy hóa, |
| 10 | 54,38–59,20 | 4,82 | không bị mất hiệu lực. Mỗi giọt đều thực sự tác động lên da đầu. Chất gel là dạng gel trong suốt, bọt rất mịn, |
| 11 | 59,20–65,81 | 6,61 | là loại mùi thơm nhạt nhạt. Hương thơm nhẹ nhàng, sau khi gội đầu cảm thấy rất sảng khoái. Nó còn được nghiên cứu chuyên biệt để đối phó với tình trạng lỗ chân lông bị xơ cứng, |
| 12 | 65,81–71,70 | 5,90 | dầu gội chống rụng tóc. Công thức nghiên cứu cốt lõi của Minoxidil có thể mở lỗ chân lông, giúp lỗ chân lông bị xơ cứng được nới lỏng, |
| 13 | 71,70–77,18 | 5,47 | chống rụng tóc từ gốc. Khi sử dụng một thời gian, rõ ràng cảm thấy tóc rụng ít hơn, chân tóc cũng vững chắc hơn, giống như tập luyện vai và lưng, |
| 14 | 77,18–83,78 | 6,60 | đường nét và sức hỗ trợ đều hiện rõ. Cơ thể cần tập luyện thường xuyên, tóc đương nhiên cũng cần được chăm sóc tốt, trạng thái và tâm trạng sẽ dần trở lại. |

### So với spike v2

- **Ranh giới thời gian:** trùng với spike ở cả hai video, đến từng 10 ms. Quy hoạch động vẫn như cũ; giới hạn 1 s sau chữ cuối không làm thay đổi gì ở hai video này, vì không có khoảng lặng nào dài quá 1 s.
- **Từ treo:** v1 có 14 chỗ cắt giữa cụm. v2 và bước này đều không còn chỗ nào.
- **Cùng các chỗ dời như v2:** "ban", "Sáng", "làm tóc,", "Tôi tin", "những", "Làm việc", "khỏe nang tóc.", "Thương hiệu Mandi này độ chuyên", "hóa,", "là", "xơ cứng,", "chống", "lưng,".
- **Khác v2 ở một chỗ:** ranh giới 7|8. v2 dời "trạng thái tóc trực tiếp ảnh" sang slot 8, nên slot 7 chỉ còn 2,3 s lời trong 4,85 s. Ở đây, "trạng thái tóc trực tiếp ảnh hưởng đến cảm giác trên camera." nằm ở slot 7: điểm cắt chỉ dời 4 từ về sau, tới hết câu.
- **Khoảng cách dời ở 7618:** điểm cắt dời tối đa 6 từ, ở cùng chỗ với v2 ("Thương hiệu Mandi này độ chuyên"), trung bình 2,1 từ.
- **Chữ trong slot:** v2 dùng chữ đã được LLM rút gọn từ v1. Ở đây là nguyên văn chữ của Vbee, nên slot dài hơn một chút; rút gọn là việc của bước sau.

## Test

Có 33 test trong `services/localization/tests/activities/test_plan_slots.py`. Dữ liệu thật của 7616 (khoảng lặng và utterance, chỉ có chữ và thời gian) nằm ở `services/localization/tests/activities/plan_slots_real.py`.

| Nhóm | Số test | Nội dung |
|---|---|---|
| Dò giọng nói trên WAV sinh bằng ffmpeg | 6 | Tone và noise với khoảng lặng biết trước (sai số ≤ 30 ms), khoảng ngừng < 0,15 s không cắt, nói liền tới hai đầu, khoảng lặng còn mở ở cuối file, phần bù của khoảng lặng |
| Không có lời | 3 | Track câm, hai từ rời trong nhạc (dưới 2 s), có tiếng nói nhưng transcript rỗng |
| Slot | 6 | Slot dài 3–10 s và chỉ cắt ở khoảng lặng; ưu tiên khoảng lặng dài; quãng lặng 5 s không bị nhập vào slot trước; nói liền không ngắt thì thành một slot; hai quãng nói liền > 60 s; slot thật của 7616 nằm trong giới hạn |
| Chữ | 16 | Giữ đủ chữ, đúng thứ tự, không mất và không lặp (7616); mọi slot của 7616 kết thúc ở dấu câu; "4,6 mg" và "146 loại" không bị tách; utterance chồng lấn (dữ liệu thật và dữ liệu tổng hợp); chuyển giao tại khoảng lặng trong phần chồng lấn; dời tới dấu câu gần nhất; gộp slot khi không có dấu câu gần; số đi liền đơn vị khi không có dấu câu; utterance không dấu câu; chỉ có dấu chấm cuối; chia theo âm tiết ("100%"); ranh giới utterance là chỗ cắt hợp lệ; tiếng nói ngoài utterance; utterance trên vocals câm; id và thứ tự |
| Activity | 2 | Đọc vocals từ store (moto), lập slot và xóa thư mục tạm; vocals bị mất thì ném `ArtifactMissingError` |

**Kiểm tra bằng mutation:** tôi phá tay 12 bất biến của code. Sau khi bổ sung test, cả 12 lần phá đều làm test đỏ. Ở lượt đầu, 5 lần phá vẫn qua được bộ test (chuyển giao tại khoảng lặng, giới hạn tìm kiếm 60 s, trọng số âm tiết, dấu chấm cuối câu, khoảng lặng còn mở ở cuối file); 5 test đã được thêm cho đúng những chỗ đó. Riêng `max(1, count_syllables(w))` đổi thành `count_syllables(w)` thì không làm test nào đỏ. Đây là một đột biến tương đương, vì một từ không rỗng luôn có ít nhất 1 âm tiết.

**Lệnh đã chạy:**
- `uv run pytest -q tests/activities/test_plan_slots.py`: 33 passed.
- `uv run pytest -q tests/activities`: 288 passed.
- `tests/architecture` và `test_secret_canary`: 63 passed.
- `ruff check` và `ruff format --check`: sạch.

## Rủi ro và lưu ý

1. **Bài hát vẫn bị coi là lời nói** (7565). Bước này không nhận ra được.
2. **Giới hạn 3–10 s là giới hạn mềm.** Ở 7665 có một quãng nói liền 11,6 s mà không có khoảng ngừng nào từ 0,15 s trở lên ở mức -30 dB, nên quy hoạch động cho ra một slot 12,2 s và một slot cuối chỉ 1,16 s. Nếu cần, có thể dò lại riêng những quãng dài hơn 10 s với ngưỡng lỏng hơn. Tôi chưa làm việc này vì nó nằm ngoài phạm vi phần này.
3. **Một test không liên quan bị chập chờn.** `test_review_render_jev.py::test_a_jev_failure_..._cleans_up[PermanentProviderError-True]` đỏ 2 trong khoảng 7 lần chạy cả bộ. Test này đếm thư mục `review-*` trong thư mục tạm chung của máy, nên đụng với các lần chạy test của agent khác chạy song song. Không phải do code của phần này: chạy riêng thì xanh, và test của tôi dùng thư mục tạm riêng.
4. **Heartbeat cần cấu hình ở workflow.** Activity gửi heartbeat mỗi 10 s trong lúc ffmpeg chạy. Khi nối vào workflow, bước sau cần đặt `heartbeat_timeout`.
5. **Có thêm một file test hỗ trợ** là `tests/activities/plan_slots_real.py`, chứa dữ liệu thật của 7616. File này nằm trong phạm vi "test của nó".
