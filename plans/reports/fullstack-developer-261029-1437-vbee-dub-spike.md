# Spike: lồng tiếng bằng Vbee STT, một lần gọi LLM và Vbee TTS, không cần Colab

Ngày: 2026-10-29. Đây là spike offline. Không sửa `src/`, không chạy pipeline, không commit.

## Kết quả

Hai video đã được render xong bằng luồng mới:

- `data/review/7616569292510948709-vbee-spike.mp4` dài 66,9 s.
- `data/review/7618513827751629177-vbee-spike.mp4` dài 84,0 s.

Cả hai dài đúng bằng bản `-g5` hiện có. Luồng chạy trọn vẹn và không có lỗi Vbee nào trong lúc lồng tiếng. Mọi câu đều vừa slot của nó, và mức tăng tốc lớn nhất là 1,09x. Phần render dùng lại renderer production (`render_file` cùng `build_captions`), lấy cover và caption box từ `r1/edit-spec.json`. Phần mix dùng lại `assemble_audio.render` của production, gồm ducking và loudnorm hai lượt ở mức -14 LUFS / -1,5 dBTP.

**Phát hiện quan trọng nhất:** Vbee STT nhận audio tiếng Trung nhưng **trả về tiếng Việt đã dịch sẵn**. Như vậy một job STT thay được cả bước transcribe lẫn bước dịch. Tài liệu Vbee không hề nhắc đến hành vi này.

**Vấn đề chất lượng lớn nhất (7618):** LLM bám quá sát gợi ý chia chữ theo tỷ lệ thời gian, nên cắt câu giữa cụm từ, ví dụ "ban | đêm chỉnh", "sức | khỏe", "ảnh | hưởng", "oxy | hóa". TTS sẽ đọc chữ "ban" ở cuối clip, rồi có một khoảng lặng mới đến "đêm". Video 7616 không gặp lỗi này, vì các slot của nó rơi gần ranh giới mệnh đề. Nguyên nhân nằm ở prompt, câu "nothing is moved … across more than a few words", chứ không phải ở luồng. Cách sửa được ghi ở phần cuối. Tôi chưa chạy lại, vì chạy lại sẽ vượt hạn mức TTS và LLM đã được duyệt.

## Bảng đo

Thời gian là wall time, tính bằng giây. Bước inputs đọc từ cache trong scratchpad nên bằng 0.

| Bước | 7616 (67 s) | 7618 (84 s) |
|---|---|---|
| STT Vbee (batch, poll 3 s) | dùng lại kết quả cũ, không đo lại | 44,8 (1 job, 13 lần poll) |
| Pauses + slots | 0,13 | 0,13 |
| LLM (1 lần gọi, không cần repair) | 7,9 | 38,7 |
| TTS (13 / 15 câu, song song 3) | 22,9 | 21,6 |
| Fit + mix | 4,0 | 5,0 |
| Render | 11,4 | 14,3 |
| **Tổng** | **46,3** (khoảng 90 nếu tính cả STT) | **124,4** |

| Chỉ số | 7616 | 7618 |
|---|---|---|
| Số slot (số câu) | 13 | 15 |
| Số lần gọi TTS, số lỗi | 13, 0 | 15, 0 |
| Độ trễ mỗi câu TTS (min / median / max) | 3,6 / 4,0 / 6,2 s | 3,2 / 3,8 / 5,3 s |
| Ký tự TTS | 1.405 | 1.957 |
| LLM: token vào / ra | 2.247 / 800 | 2.831 / 1.074 |
| LLM: chi phí | $0,0074 | $0,0087 |
| Câu không vừa slot ở 1,15x | 0 | 0 |
| Mức tăng tốc lớn nhất | 1,00x | 1,09x |

**Tổng số lần gọi trong spike:**

- **STT:** 1 job mới cho 7618. Kết quả STT của 7616 được dùng lại.
- **TTS:** 3 lần gọi thử và 28 lần gọi cho câu thật. Trong 3 lần thử có 1 lần sync bị từ chối, 1 lần async bị mất (xem phần chế độ TTS) và 1 lần async thành công.
- **LLM:** 2 lần gọi, tổng $0,016. Model là `deepseek/deepseek-v4-pro`, cùng thiết lập answer-only như Localizer.
- **Điểm Vbee:** khoảng (3.362 + 2 × 232) × 2 ≈ 7,7 nghìn điểm. Hệ số 2 là vì giọng clone có `credit_factor` = 2.

## Chế độ TTS

**Batch (`mode=async`) chạy được, realtime (`mode=sync`) không.**

- Giọng trong `VBEE_VOICE_ID` là giọng **PERSONAL** (giọng clone, nữ, vi-VN, `credit_factor` 2). Tôi tra điều này qua `GET /api/public/v1/voices`, không tốn phí. Tài liệu ghi realtime chỉ chạy 5 giọng preset, nên lần gọi sync bị từ chối là đúng như dự kiến. Tôi không lưu lại nội dung lỗi của lần gọi sync đầu tiên, và không gọi lại để giữ đúng hạn mức 3 lần thử.
- **Batch với webhook giả.** Dùng `webhookUrl` giả (`https://example.com/...`) rồi tự poll `GET /v1/tts/requests/{id}` mỗi giây thì chạy tốt.
- **Link audio phải theo redirect.** Lần thử async đầu tiên thất bại ở phía mình, gần như chắc chắn vì link audio trả về redirect mà client chưa theo, nên có thể Vbee vẫn đã tính phí lần đó. Sau khi bật `follow_redirects` thì mọi lần gọi đều thành công.
- **Định dạng.** Vbee trả WAV PCM 16-bit, mono, 24 kHz, là tần số mặc định của giọng này. Tôi không ép `sampleRate`, vì giọng clone có thể không nhận 44,1 kHz. Clip được cắt khoảng lặng ở hai đầu rồi resample lên 44,1 kHz stereo.
- **Đồng thời.** Chạy 3 câu song song không gặp lỗi 429. Mỗi câu mất 3,2–6,2 s, gồm hàng đợi, poll và tải về.
- **Tốc độ đọc.** Đo trên mẫu `voice_rate.SAMPLE_TEXT` được 5,40 âm tiết/giây. Mẫu này có ngắt câu, nên khi đọc câu thật giọng nhanh hơn, khoảng 6 âm tiết/giây. Đó là lý do hầu hết câu đều thừa chỗ.

## Phát hiện khoảng lặng và slot

- **Tham số:** `silencedetect` với ngưỡng -30 dB và thời lượng tối thiểu 0,15 s. Mức này bắt được cả những chỗ ngắt hơi ngắn trong lời nói liên tục, nên có nhiều điểm cắt để chọn. Mức chặt hơn (-35 dB / 0,25 s) không tìm thấy khoảng lặng nào trong đoạn 31–58 s của 7616, và sẽ buộc phải có một slot dài hơn 10 s.
- **Cách gộp thành slot:** một quy hoạch động nhỏ chọn điểm cắt sao cho mỗi slot dài 3–10 s, gần 6 s, và ưu tiên cắt ở khoảng lặng dài.
- **Kết quả:** các slot dài 3,7–7,3 s (7616) và 4,1–7,1 s (7618).
- **Thời lượng slot:** tính từ lúc bắt đầu nói của slot này đến lúc bắt đầu của slot sau, tức là gồm cả khoảng lặng phía sau. Slot cuối được thêm tối đa 1 s.

## Đồng bộ

Tôi so ranh giới slot (điểm bắt đầu nói và điểm kết thúc nói) với `r0/transcript.json` của whisperx.

| | 7616 | 7618 |
|---|---|---|
| Số segment whisperx / số ranh giới slot | 8 / 26 | 11 / 30 |
| Ranh giới whisperx → ranh giới slot gần nhất: median / max | **73 / 3.433 ms** | **244 / 2.410 ms** |
| Ranh giới slot → ranh giới segment whisperx gần nhất: median / max | 2.118 / 5.238 ms | 2.074 / 4.062 ms |
| Ranh giới slot → mép từ whisperx gần nhất: median / max | 104 / 2.681 ms | 86 / 244 ms |

**Cách đọc bảng:**

- **Gần như mọi chỗ whisperx ngắt segment đều có một ranh giới slot ngay đó** (median 73 và 244 ms). Vậy khoảng lặng phát hiện được khớp với chỗ người nói thật sự ngắt câu.
- Chiều ngược lại cho median khoảng 2 s. Lý do là slot dày gấp khoảng 1,5 lần segment whisperx: mỗi segment whisperx dài 6–10 s và nối liền nhau. Con số này không phải lỗi đồng bộ.
- So với mép từ, median là 86–104 ms.
- Script cũng đếm được 15 ranh giới slot nằm "bên trong một từ" whisperx. Số này không có ý nghĩa: whisperx gán thời gian cho từng chữ Hán và kéo dài chữ qua cả khoảng lặng.
- Mỗi câu lồng tiếng bắt đầu đúng lúc người nói gốc bắt đầu (lấy từ vocals), vì slot được dựng như vậy.
- **Đồng bộ nội dung thì chưa đảm bảo.** Vbee chỉ cho timestamp theo utterance (7616 có 3 utterance dài 9–36 s, 7618 có 2 utterance dài 60 s và 24 s). Vì vậy việc chữ nào thuộc slot nào chỉ là ước lượng tỷ lệ cộng với phán đoán của LLM.

## Các câu thoại (để theo dõi khi xem)

Cột "Clip" là độ dài audio TTS sau khi cắt lặng. Cột "Tốc" là hệ số tăng tốc bằng atempo.

### 7616569292510948709

| Slot | Thời gian (s) | Clip (s) | Tốc | Câu |
|---|---|---|---|---|
| 0 | 0,00–5,35 | 4,19 | 1,00 | Ba mươi tuổi trông như hai mươi tuổi, sức sống hướng thượng, ánh sáng tự túc của mình thật sự không bao giờ tắt. |
| 1 | 5,35–10,50 | 4,72 | 1,00 | Ăn sạch sẽ, tập thể dục đều đặn, nghỉ ngơi đúng giờ, và một điều tôi không bao giờ bỏ qua là bổ sung thích hợp. |
| 2 | 10,50–14,81 | 3,18 | 1,00 | Đặc biệt là cứ một trăm phụ nữ thì bảy mươi người thiếu sắt, nên càng sớm càng tốt. |
| 3 | 14,81–18,88 | 3,96 | 1,00 | Bổ sung sắt, tôi chỉ xem bốn điểm: nguồn sắt, hàm lượng, cảm giác khi uống, và tính an toàn. |
| 4 | 18,88–22,88 | 3,22 | 1,00 | So sánh vô số loại, tôi luôn mua lại là viên ngậm bổ sung sắt của Five Girls Doctor. |
| 5 | 22,88–27,26 | 3,72 | 1,00 | Nó khác với các loại sắt thông thường trên thị trường, sử dụng sắt hữu cơ chất lượng cao cấp năm, |
| 6 | 27,26–30,97 | 2,97 | 1,00 | dịu nhẹ, dễ hấp thụ, không gây kích ứng, còn kết hợp với huyết bạch đa peptide, |
| 7 | 30,97–38,29 | 6,30 | 1,00 | anthocyanin và nồng độ cao vitamin C, hiệu suất đặc biệt cao. Chỉ một viên nhỏ đã có bốn phẩy sáu mi-li-gam sắt, ăn ba viên mỗi ngày vừa đủ nhu cầu, |
| 8 | 38,29–44,70 | 5,45 | 1,00 | tương đương nửa cân kỷ tử, một cân thịt bò, hai cân táo đỏ. Bình thường không ăn được nhiều như vậy, một viên có thể giúp bạn giải quyết. |
| 9 | 44,70–49,06 | 3,03 | 1,00 | Vị mật ong chua ngọt rất dễ duy trì, lượng calo tương đương với một quả táo, |
| 10 | 49,06–54,95 | 5,51 | 1,00 | như tôi là người thường xuyên trong giai đoạn quản lý cũng không cần lo lắng. An tâm hơn là có một trăm bốn mươi sáu loại báo cáo không chứa hormone, |
| 11 | 54,95–60,12 | 4,31 | 1,00 | bà bầu, kỳ kinh nguyệt, cho con bú đều có thể ăn thoải mái. Phụ nữ ăn sắt thật sự phải sớm. |
| 12 | 60,12–66,87 | 5,98 | 1,00 | Hiện tại thương hiệu còn có hoạt động, trước đây một chai giá giờ nhận được ba lon lớn. Yêu bản thân, bắt đầu từ mỗi ngày ba viên kẹo bổ sung sắt. |

### 7618513827751629177

Các chỗ cắt giữa cụm từ được ghi là "cắt giữa cụm".

| Slot | Thời gian (s) | Clip (s) | Tốc | Câu |
|---|---|---|---|---|
| 0 | 0,00–5,82 | 5,72 | 1,00 | Bây giờ là hai giờ sáng, cuối cùng tôi cũng đã chỉnh xong video sẽ đăng vào ngày mai. Những người làm tự do đều hiểu, ban ngày quay, ban (cắt giữa cụm) |
| 1 | 5,82–10,39 | 4,36 | 1,00 | đêm chỉnh, thức khuya đã trở thành chuyện thường ngày. Mỗi ngày làm thêm giờ đã thành thói quen của tôi. Sáng (cắt giữa cụm) |
| 2 | 10,39–15,49 | 4,73 | 1,00 | dậy đã bị cái này, tóc bị lên một lớp. Đầu lợn càng ngày càng xinh đẹp, một chỗ một chỗ. Lần trước đi (cắt giữa cụm) |
| 3 | 15,49–20,56 | 4,80 | 1,00 | làm tóc, thợ làm tóc của tôi đã nói rồi. Cảm thấy mấy năm nay đường chân tóc của tôi cao lên rất nhiều. Tôi tin (cắt giữa cụm) |
| 4 | 20,56–24,61 | 3,74 | 1,00 | rằng những người trẻ làm việc thêm giờ trong văn phòng, những học sinh lướt điện thoại vào ban đêm, những |
| 5 | 24,61–29,52 | 4,93 | 1,00 | người làm video giống tôi, đồng nghiệp chắc cũng giống tôi, người thức khuya đều đang trả tiền cho chứng rụng tóc. Làm việc |
| 6 | 29,52–36,65 | 6,59 | 1,00 | thêm giờ lâu dài sẽ khiến nang tóc mất sức sống, dần trở nên cứng. Chúng ta thường nghĩ là do áp lực lớn, thực ra đều là do thức khuya âm thầm phá hủy sức (cắt giữa cụm) |
| 7 | 36,65–41,50 | 4,67 | 1,00 | khỏe nang tóc. Đặc biệt là tôi là blogger thường xuyên phải lên hình, đường chân tóc, trạng thái tóc trực tiếp ảnh (cắt giữa cụm) |
| 8 | 41,50–48,04 | 6,65 | 1,02 | hưởng đến cảm giác trên camera. Rụng tóc thực sự làm tăng lo lắng, vì vậy sản phẩm chăm sóc tóc tôi chỉ xem độ chuyên nghiệp. Thương hiệu Mandi này độ chuyên (cắt giữa cụm) |
| 9 | 48,04–54,38 | 5,90 | 1,00 | nghiệp không cần tôi nói nhiều. Chai dầu gội chống rụng tóc của nó, điều tôi thích nhất là chai đóng chân không, giữ thành phần chính không bị oxy (cắt giữa cụm) |
| 10 | 54,38–59,20 | 5,27 | 1,09 | hóa, không bị mất hiệu lực. Mỗi giọt đều thực sự tác động lên da đầu. Chất gel trong suốt, bọt rất mịn, là |
| 11 | 59,20–65,81 | 6,72 | 1,02 | loại mùi thơm nhạt nhạt. Hương thơm nhẹ nhàng, sau khi gội đầu cảm thấy rất sảng khoái. Nó còn được nghiên cứu chuyên biệt để đối phó với tình trạng lỗ chân lông bị |
| 12 | 65,81–71,70 | 6,23 | 1,06 | xơ cứng, dầu gội chống rụng tóc. Công thức nghiên cứu cốt lõi của Minoxidil có thể mở lỗ chân lông, giúp lỗ chân lông bị xơ cứng được nới lỏng, chống |
| 13 | 71,70–77,18 | 5,78 | 1,06 | rụng tóc từ gốc. Khi sử dụng một thời gian, rõ ràng cảm thấy tóc rụng ít hơn, chân tóc cũng vững chắc hơn, giống như tập luyện vai và |
| 14 | 77,18–83,94 | 6,35 | 1,00 | lưng, đường nét và sức hỗ trợ đều hiện rõ. Cơ thể cần tập luyện thường xuyên, tóc cũng cần được chăm sóc tốt, trạng thái và tâm trạng sẽ dần trở lại. |

LLM gần như không rút gọn câu, vì ước lượng cho thấy câu nào cũng đã vừa slot.

## Cần gì để đưa vào pipeline thật

1. **Sửa prompt chia câu.** Yêu cầu mỗi câu bắt đầu và kết thúc ở ranh giới mệnh đề, và cho phép dời điểm cắt tới một cụm từ so với gợi ý. Kiểm tra bằng code rằng câu không kết thúc bằng một từ "treo" (như "ban", "sức", "của", "những"). Nếu vi phạm thì gọi repair một lần. Sau đó chạy lại 7618 (1 lần gọi LLM, khoảng 15 lần TTS) để xác nhận.
2. **Activity `vbee_stt`.** Gửi vocals 16 kHz mono, poll mỗi 3 s, timeout khoảng 5 phút. Bước này thay cả `transcribe` lẫn phần dịch của Localizer. Output là danh sách utterance, không phải `TranscriptDocument` có `words`.
3. **Activity `plan_slots`.** Gồm silencedetect và quy hoạch động, là code thuần, test được. Bước này vẫn cần vocals đã tách, tức là cần Demucs. Spike dùng lại `r0/vocals.wav`, nên chưa kiểm chứng việc tách vocals có chạy được ngoài Colab không.
4. **Agent "segmenter".** Một request gồm utterance, slot, ngân sách âm tiết và gợi ý chia chữ. Agent này thay Localizer và Dubber. Cần giữ các phần đang có: dữ liệu không tin cậy đặt trong `<untrusted_video_text>`, glossary, kiểm tra glyph, `answer_only_settings`.
5. **`VbeeTtsProvider` (batch).** Gồm webhook (giả hoặc thật), poll mỗi 1 s, tải link với `follow_redirects` ngay (link sống 3 phút), chấp nhận WAV 24 kHz mono, xếp 429 và 5xx vào `TransientProviderError`, 400 và 401 vào lỗi vĩnh viễn, và giới hạn song song khoảng 3.
6. **Đo `voice_rate`.** Cần đo trên câu thật, không đo trên mẫu có ngắt câu. Giọng clone đọc khoảng 6 âm tiết/giây, còn mẫu cho 5,4.
7. **`assemble_audio` và `layout_captions` gần như dùng nguyên.** Chỉ cần nhận đầu vào là (slot_start, clip, ratio) thay cho dub-segments.
8. **Đánh giá.** Chạy eval fidelity (Jev) trên bản dịch của Vbee so với whisperx kèm Localizer trên vài chục video, trước khi bỏ Colab.

## Rủi ro

| Rủi ro | Mức | Ghi chú |
|---|---|---|
| Việc dịch zh→vi trong STT không có trong tài liệu | Cao | Vbee có thể đổi hoặc tắt bất cứ lúc nào, và mình không điều khiển được (không có tham số ngôn ngữ, không có glossary). |
| Chất lượng dịch | Cao | Có các lỗi như "Đầu lợn càng ngày càng xinh đẹp", "ánh sáng tự túc", "Mandi" (tên thương hiệu đoán sai?), "huyết bạch đa peptide". Pipeline hiện tại có glossary và repair, luồng này thì không. |
| Thời gian chỉ ở mức utterance | Cao | Một utterance dài 60 s, nên việc gán chữ vào slot là ước lượng. Nội dung có thể lệch hình vài giây, nhất là khi nhịp nói không đều. |
| Cắt câu giữa cụm từ | Cao, nhưng sửa được | Xem 7618. Cần sửa prompt và thêm kiểm tra bằng code. |
| Độ trễ batch TTS không có cam kết | Trung bình | Hôm nay mỗi câu 3–6 s, cả video khoảng 22 s ở mức song song 3. Changelog tháng 10 của Vbee vừa sửa lỗi "API chậm". |
| Realtime không chạy được với giọng clone | Trung bình | Bắt buộc phải dùng batch, kèm webhook và poll. |
| Thời gian LLM dao động | Thấp | Hai lần gọi cùng loại mất 7,9 s và 38,7 s. |
| STT batch | Trung bình | 45 s cho 84 s audio, chiếm khoảng một phần ba tổng thời gian. Không có SLA. |
| Chi phí tăng gấp đôi vì giọng clone (`credit_factor` 2) | Thấp | Khoảng 7,7 nghìn điểm cho spike, tương đương vài nghìn đồng. |
| Điều khoản dữ liệu | Trung bình | Vbee được dùng input và output để cải tiến sản phẩm, không có lựa chọn opt-out. Người dùng cần quyết định. |

## File

- Script: `services/localization/eval/vbee_dub_spike.py`. Đã qua `ruff check` và `ruff format --check`.
- Dữ liệu trung gian (`slots.json`, `lines.json` kèm prompt, `tts-calls.json`, `result.json`, clip) nằm ở `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/07f62d39-b5c7-4084-a633-0d769a53fb9d/scratchpad/vbee-dub/`. Tôi đã quét các file JSON, không có token, app id hay voice id nào.

## Câu hỏi còn mở

1. Có duyệt thêm 1 lần gọi LLM và khoảng 15 lần TTS để chạy lại 7618 với prompt đã sửa không?
2. Lỗi trả về khi gọi sync với giọng clone chưa được ghi lại. Có cần một lần gọi nữa để lưu nội dung lỗi không?
3. Demucs có đang chạy trên Colab không? Nếu có thì luồng này vẫn chưa hoàn toàn bỏ được Colab.

## v2 của 7618 (sửa chỗ cắt câu)

File: `data/review/7618513827751629177-vbee-spike-v2.mp4` (84,0 s). Bản v1 vẫn giữ nguyên để so sánh.

### Kết quả

**Video v2 không dùng câu do LLM viết.** Prompt v2 đã sửa theo yêu cầu, nhưng cả hai lần gọi LLM được phép đều trả về câu bị trôi khỏi đúng chỗ trong video và bỏ mất phần cuối. Vì vậy bản v2 dùng lại nguyên các chữ của v1 và chỉ cắt lại câu bằng code, tại dấu câu gần nhất. Kết quả là không còn câu nào kết thúc giữa cụm từ. Không có lỗi dịch nào được sửa: bản v2 vẫn còn "Đầu lợn càng ngày càng xinh đẹp" và "Mandi".

### Hai lần gọi LLM

Prompt v2 (`V2_INSTRUCTIONS` trong script) yêu cầu:
- câu chỉ được kết thúc ở cuối cụm từ hoặc cuối câu;
- được dời chữ sang slot liền kề, và được để slot ngắn hoặc trống thay vì cắt ngang cụm từ;
- chỉ sửa chỗ dịch hỏng nghĩa rõ ràng, và ghi lại từng chỗ sửa trong `fixes`.

Code kiểm tra hai điều: câu có kết thúc bằng dấu câu không, và câu có quá dài so với slot không. Ở lần gọi thứ hai tôi thêm một kiểm tra **trôi nội dung**: dưới 40% số từ của một câu trùng với phần lời được ước lượng cho slot đó và hai slot kề bên. Tôi đã thử kiểm tra này trên các câu v1: không câu nào bị báo trôi, còn 14 câu bị báo cắt giữa cụm.

- **Lần 1 (26 s):** mọi câu đều kết thúc đúng chỗ, nhưng LLM rút gọn mạnh ở nửa đầu nên nội dung trôi dần về phía trước. Đến slot 14 (77–84 s), LLM đang đọc phần "Chất gel trong suốt…", vốn nói ở khoảng 59 s. Khoảng 20 s nội dung cuối bị bỏ mất: "Hương thơm…", Minoxidil, "tóc rụng ít hơn…", câu kết. Kiểm tra trôi báo lỗi ở các slot 8, 10, 11, 13 và 14.
- **Lần 2 (26 s):** tôi gửi lại câu trả lời cũ kèm danh sách lỗi. LLM vẫn trôi ở các slot 8, 11, 13, 14 và vẫn bỏ phần cuối.
- **Ghi lại chỗ sửa dịch không đáng tin.** LLM chỉ ghi một chỗ sửa: bỏ "đồng nghiệp chắc cũng giống tôi". Trong khi đó nó bỏ câu "Đầu lợn càng ngày càng xinh đẹp" mà không ghi lại.
- Cả hai câu trả lời được lưu trong `lines-v2.first-call.json` và `lines-v2.json` (khóa `llm_texts`).

Dựng một bản từ câu bị trôi sẽ cho ra video lệch nội dung tới 15–20 s và im lặng ở đoạn cuối. Quy tắc không cho gọi LLM thêm lần nào nữa. Vì vậy tôi dùng bước dự phòng bằng code (`resplit_at_phrases`): giữ nguyên chữ của v1, và dời mỗi chỗ cắt giữa hai slot tới dấu câu gần nhất. Việc này chỉ dùng số lần gọi TTS dành cho câu cuối cùng (15 câu), không tốn thêm.

### Các cụm đã dời (so với v1)

| Ranh giới slot | Cụm được dời |
|---|---|
| 0→1 | "ban" (đêm chỉnh) sang slot 1 |
| 1→2 | "Sáng" sang slot 2 |
| 2←3 | "làm tóc," về slot 2 |
| 3→4 | "Tôi tin" sang slot 4 |
| 4→5 | "những" sang slot 5 |
| 5→6 | "Làm việc" sang slot 6 |
| 6←7 | "khỏe nang tóc." về slot 6 |
| 7→8 | "trạng thái tóc trực tiếp ảnh" sang slot 8 |
| 8→9 | "Thương hiệu Mandi này độ chuyên" sang slot 9 |
| 9←10 | "hóa," về slot 9 |
| 10→11 | "là" sang slot 11 |
| 11←12 | "xơ cứng," về slot 11 |
| 12→13 | "chống" sang slot 13 |
| 13←14 | "lưng," về slot 13 |

Mỗi lần dời tối đa 6 từ. Slot 7 vì vậy ngắn hơn: clip dài 2,3 s trong slot 4,85 s.

### Các câu v2

| Slot | Thời gian (s) | Clip (s) | Tốc | Câu |
|---|---|---|---|---|
| 0 | 0,00–5,82 | 5,56 | 1,00 | Bây giờ là hai giờ sáng, cuối cùng tôi cũng đã chỉnh xong video sẽ đăng vào ngày mai. Những người làm tự do đều hiểu, ban ngày quay, |
| 1 | 5,82–10,39 | 4,26 | 1,00 | ban đêm chỉnh, thức khuya đã trở thành chuyện thường ngày. Mỗi ngày làm thêm giờ đã thành thói quen của tôi. |
| 2 | 10,39–15,49 | 5,20 | 1,02 | Sáng dậy đã bị cái này, tóc bị lên một lớp. Đầu lợn càng ngày càng xinh đẹp, một chỗ một chỗ. Lần trước đi làm tóc, |
| 3 | 15,49–20,56 | 3,92 | 1,00 | thợ làm tóc của tôi đã nói rồi. Cảm thấy mấy năm nay đường chân tóc của tôi cao lên rất nhiều. |
| 4 | 20,56–24,61 | 3,79 | 1,00 | Tôi tin rằng những người trẻ làm việc thêm giờ trong văn phòng, những học sinh lướt điện thoại vào ban đêm, |
| 5 | 24,61–29,52 | 4,36 | 1,00 | những người làm video giống tôi, đồng nghiệp chắc cũng giống tôi, người thức khuya đều đang trả tiền cho chứng rụng tóc. |
| 6 | 29,52–36,65 | 7,27 | 1,02 | Làm việc thêm giờ lâu dài sẽ khiến nang tóc mất sức sống, dần trở nên cứng. Chúng ta thường nghĩ là do áp lực lớn, thực ra đều là do thức khuya âm thầm phá hủy sức khỏe nang tóc. |
| 7 | 36,65–41,50 | 2,33 | 1,00 | Đặc biệt là tôi là blogger thường xuyên phải lên hình, đường chân tóc, |
| 8 | 41,50–48,04 | 6,16 | 1,00 | trạng thái tóc trực tiếp ảnh hưởng đến cảm giác trên camera. Rụng tóc thực sự làm tăng lo lắng, vì vậy sản phẩm chăm sóc tóc tôi chỉ xem độ chuyên nghiệp. |
| 9 | 48,04–54,38 | 6,95 | 1,10 | Thương hiệu Mandi này độ chuyên nghiệp không cần tôi nói nhiều. Chai dầu gội chống rụng tóc của nó, điều tôi thích nhất là chai đóng chân không, giữ thành phần chính không bị oxy hóa, |
| 10 | 54,38–59,20 | 4,62 | 1,00 | không bị mất hiệu lực. Mỗi giọt đều thực sự tác động lên da đầu. Chất gel trong suốt, bọt rất mịn, |
| 11 | 59,20–65,81 | 7,17 | 1,09 | là loại mùi thơm nhạt nhạt. Hương thơm nhẹ nhàng, sau khi gội đầu cảm thấy rất sảng khoái. Nó còn được nghiên cứu chuyên biệt để đối phó với tình trạng lỗ chân lông bị xơ cứng, |
| 12 | 65,81–71,70 | 5,57 | 1,00 | dầu gội chống rụng tóc. Công thức nghiên cứu cốt lõi của Minoxidil có thể mở lỗ chân lông, giúp lỗ chân lông bị xơ cứng được nới lỏng, |
| 13 | 71,70–77,18 | 6,02 | 1,10 | chống rụng tóc từ gốc. Khi sử dụng một thời gian, rõ ràng cảm thấy tóc rụng ít hơn, chân tóc cũng vững chắc hơn, giống như tập luyện vai và lưng, |
| 14 | 77,18–83,94 | 6,18 | 1,00 | đường nét và sức hỗ trợ đều hiện rõ. Cơ thể cần tập luyện thường xuyên, tóc cũng cần được chăm sóc tốt, trạng thái và tâm trạng sẽ dần trở lại. |

### Bản dịch đã sửa

Không có. Các lỗi dịch của Vbee vẫn còn nguyên trong v2: "Đầu lợn càng ngày càng xinh đẹp, một chỗ một chỗ", "Sáng dậy đã bị cái này", "Mandi", "trả tiền cho chứng rụng tóc".

### Thời gian và số lần gọi

| Bước | v2 |
|---|---|
| STT | dùng lại kết quả của v1 (44,8 s), không gọi lại |
| Khoảng lặng và slot | 0,09 s |
| LLM | 2 lần gọi, tổng 52,4 s (26,2 s + 26,2 s), 7.644 token vào và 1.713 token ra, $0,019 |
| TTS | 21,2 s, 15 lần gọi, 0 lỗi, mỗi câu 2,8 / 3,8 / 4,5 s (min / median / max), 1.957 ký tự |
| Mix | 5,2 s |
| Render | 15,1 s |
| Tổng | 138,9 s (gồm cả thời gian STT đo ở v1) |

Không câu nào vượt slot, mức tăng tốc lớn nhất là 1,10x, và không slot nào để trống. Ranh giới slot giữ nguyên như v1, nên các số đồng bộ cũng giống v1.

### Rút ra

- **Cắt lại câu theo dấu câu bằng code là rẻ và chắc chắn.** Cách này chỉ cần dời tối đa 6 từ mỗi chỗ, nên làm ở dạng code, không cần LLM.
- **Không nên giao cho LLM việc vừa chia câu vào slot vừa rút gọn trong một lần gọi.** Với utterance dài 60 s, LLM rút gọn rồi trôi dần và bỏ mất phần cuối, kể cả khi đã được nhắc lại. Nếu muốn LLM rút gọn hay sửa dịch, nên cho nó làm từng slot một trên đoạn chữ đã được chia sẵn bằng code, rồi kiểm tra bằng code.
- **Lỗi dịch của Vbee STT là vấn đề nằm ngoài cách chia câu.** LLM sửa lỗi dịch mà không có bản tiếng Trung đi kèm thì không đáng tin. Nó bỏ cả câu mà không ghi lại. Việc sửa dịch cần nguồn tiếng Trung, tức là quay lại cần một bước STT tiếng Trung.
