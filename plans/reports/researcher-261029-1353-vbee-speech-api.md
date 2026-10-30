# Vbee Speech API (TTS và STT): có thay được VoiceStudio trên Colab không

Ngày: 2026-10-29. Chỉ đọc tài liệu công khai. Không đăng ký, không gọi endpoint trả phí, không gửi credential.

## Kết luận

Vbee **không thay được STT**. Tài liệu STT API không có tham số ngôn ngữ, mọi ví dụ đều là tiếng Việt, tính năng tạo phụ đề trên Studio chỉ cho chọn tiếng Việt hoặc tiếng Anh, và timestamp chỉ ở mức câu (utterance), không có mức từ hay ký tự. Vì vậy STT của Vbee chỉ dùng được để nghe lại bản lồng tiếng tiếng Việt của mình, và ngay việc đó cũng chỉ thô vì không có timestamp theo từ.

Vbee **chỉ có thể thay TTS một phần, và có điều kiện**. Có hai luồng TTS:

- **Realtime (sync)** trả về bytes audio ngay, TTFB khoảng 500 ms, tối đa 300 ký tự mỗi lần. Luồng này hợp với các đoạn 80 ký tự của mình, nhưng chỉ chạy được 5 giọng nữ có sẵn, **không dùng được giọng clone**. Một dự án bên thứ ba còn ghi nhận lỗi `This feature is not supported in user package` khi gọi sync vào ngày 2026-10-23.
- **Batch (async)** dùng được mọi giọng, kể cả giọng clone tiếng Việt, nhưng bắt buộc có `webhookUrl`. Mỗi câu thành một job, rồi phải poll và tải audio từ một link chỉ sống 3 phút. Độ trễ là "theo hàng đợi hệ thống" và không có cam kết nào.

Đề xuất: **chưa chuyển**. Nếu người dùng duyệt chi một khoản nhỏ thì chỉ nên thử TTS realtime bằng một giọng preset trên đúng các câu khó của mình. STT giữ nguyên VoiceStudio.

## Bảng tóm tắt

| Hạng mục | Kết quả | Mức phù hợp |
|---|---|---|
| **TTS** | Realtime: `POST https://api.vbee.vn/v1/tts` với `mode=sync`, ≤300 ký tự, trả bytes mp3/wav/pcm, chỉ 5 giọng nữ. Batch: `mode=async`, ≤100.000 ký tự, bắt buộc `webhookUrl`, link audio hết hạn sau 3 phút. Speed 0.25–1.9. sampleRate từ 8k đến 48k. Không có SSML hay pitch trong API. Clone chỉ tiếng Việt, tạo trên Studio, mỗi lần dùng giọng clone nhanh bị trừ gấp đôi điểm. | Một phần. Realtime nhanh nhưng không có giọng clone. Batch có giọng clone nhưng chậm và phức tạp. |
| **STT** | Batch (WAV <100 MB, phản hồi tối đa 4 giờ), Realtime (<10 s), Streaming WebSocket (8k/16k PCM, tối đa 90 s). Không có tham số ngôn ngữ. Timestamp chỉ theo utterance (`text`, `startTime`, `endTime`). | Không dùng được cho tiếng Trung. Chỉ dùng được để kiểm tra tiếng Việt, và chỉ ở mức câu. |
| **Giá** | Từ 2026-01-30 Studio và API dùng chung một bảng giá. Từ 2026-02-28 tính bằng điểm, 1 ký tự = 1 điểm. Gói thường chỉ dùng ½ quota cho API, gói VIP+ dùng 100%, gói miễn phí không có API. Mua thêm 5 luồng đồng thời giá 100.000đ. Đơn giá công khai gần nhất là 350đ/1.000 ký tự (gói Tùy chỉnh, 05/2025). Giá STT cho API chưa công bố. | Rẻ, nếu giá 05/2025 vẫn còn đúng. Phải mua gói trả phí mới có API. |
| **Fit với code** | Bọc được sau `TtsProvider.synthesize(text, voice) -> bytes`. Realtime đơn giản. Batch cần webhook công khai, poll, tải audio ngay và chuyển về WAV. STT không khớp `SttProvider` vì thiếu ngôn ngữ và thiếu `words`. | TTS realtime: adapter nhỏ. TTS batch: adapter vừa. STT: không đáng làm. |

## Chi tiết

### 1. Endpoint và xác thực

- **Nguồn đã dùng.** Trang `https://vbee.vn/api-docs` là app Next.js, nhưng trỏ sang GitBook `https://api-docs.vbee.vn/`. Tôi đọc bản markdown của GitBook qua [llms.txt](https://api-docs.vbee.vn/llms.txt), thêm `.md` vào cuối mỗi URL. Ngoài ra tôi dùng help center [help.vbee.vn/llms.txt](https://help.vbee.vn/llms.txt), [policy.vbee.vn](https://policy.vbee.vn/chinh-sach-va-dieu-khoan.md), trang [vbee.vn/pricing](https://vbee.vn/pricing), và mã nguồn bên thứ ba trên GitHub.
- **Base URL.** API mới nằm ở `https://api.vbee.vn/v1/...`. Danh sách giọng nằm ở `https://vbee.vn/api/public/v1/voices`. API cũ (legacy) nằm ở `https://vbee.vn/api/v1/tts`. Nguồn: [Batch API](https://api-docs.vbee.vn/vbee-api/text-to-speech/batch-api.md), [Get list voices](https://api-docs.vbee.vn/vbee-api/voices/get-list-voices.md), [Create speech (legacy)](https://api-docs.vbee.vn/text-to-speech-lagacy/create-speech.md).
- **Xác thực.** Gửi hai header `Authorization: Bearer <token>` và `App-Id: <app-id>`. Token là JWT, sinh ra khi tạo "ứng dụng" tại `studio.vbee.vn/apps`. Lúc tạo có thể chọn hạn dùng 7, 30, 60, 90 ngày, vĩnh viễn hoặc tùy chỉnh. Khi token hết hạn phải tạo ứng dụng mới. Nguồn: [Tạo ứng dụng App ID và Token](https://api-docs.vbee.vn/tao-ung-dung-app-id-va-token.md).
- **Sync và async.**
  - TTS realtime trả về luồng bytes audio theo chunk ([Realtime API](https://api-docs.vbee.vn/vbee-api/text-to-speech/realtime-api.md)).
  - TTS batch trả `{requestId, status: "PROCESSING"}`. Sau đó Vbee POST kết quả về `webhookUrl`, hoặc mình tự poll `GET /v1/tts/requests/{requestId}` để lấy `audioLink` ([Get request](https://api-docs.vbee.vn/vbee-api/text-to-speech/get-request.md), [Callback](https://api-docs.vbee.vn/vbee-api/text-to-speech/callback-api.md)).
- **Cách trả audio.**
  - Realtime trả thẳng bytes.
  - Batch trả một URL tải audio, **chỉ hợp lệ 3 phút**. Audio được giữ 3 ngày. Muốn có link mới thì gọi lại Get request. Nguồn: [Batch API](https://api-docs.vbee.vn/vbee-api/text-to-speech/batch-api.md).
- **Lỗi.** Lỗi trả JSON dạng `{"error":{"code","message"}}`. Các mã gồm 401 `UNAUTHORIZED`, 400 `BAD_REQUEST` (bao gồm cả trường hợp hết credit), 429 `TTS_CCR_MAX_LIMIT_REACHED`, 500 `TTS_SPEND_CREDITS_FAILED` và 500 `INTERNAL_SERVER_ERROR`. Nguồn: [Realtime API](https://api-docs.vbee.vn/vbee-api/text-to-speech/realtime-api.md).

### 2. TTS

- **Giọng tiếng Việt.** Ví dụ trong tài liệu có 20 mã giọng `vi-VN`, gồm Hà Nội, Sài Gòn và Huế, cả nam lẫn nữ. Ví dụ: `hn_female_ngochuyen_full_48k-fhg`, `sg_male_minhhoang_full_48k-fhg`, `hue_female_huonggiang_full_48k-fhg`, `hn_male_thanhlong_talk_48k-fhg`. Mỗi giọng có `credit_factor` (hệ số điểm). Có thể lọc theo `voice_ownership` = VBEE, COMMUNITY hoặc PERSONAL (giọng clone của mình). Nguồn: [Get list voices](https://api-docs.vbee.vn/vbee-api/voices/get-list-voices.md). FAQ ghi 400+ giọng và 50+ ngôn ngữ ([FAQ](https://help.vbee.vn/docs/faq.md)).
- **Realtime chỉ chạy 5 giọng:** HN Ngọc Huyền, SG Tường Vy, HN Mai Phương, SG Lan Trinh, SG Thảo Trinh. Cả năm đều là giọng nữ ([Realtime API](https://api-docs.vbee.vn/vbee-api/text-to-speech/realtime-api.md)). Thông báo 04/2026 thậm chí chỉ ghi "hỗ trợ cho giọng Ngọc Huyền" ([Bản cập nhật 04/2026](https://help.vbee.vn/docs/thong-bao/ban-cap-nhat-thang-04-2026.md)).
- **Clone giọng.**
  - Chỉ hỗ trợ **tiếng Việt**.
  - Giọng clone dùng được trong "Chuyển văn bản, Dự án, API".
  - Mỗi lần dùng giọng clone nhanh bị trừ x2 ký tự.
  - Có hai cách. Clone nhanh (zero-shot) cần 10–30 giây audio và xong sau 1–2 phút. Clone chuyên nghiệp cần từ 5 phút thu âm trở lên.
  - Vbee tự nhận clone nhanh có "tỷ lệ lỗi đọc cao hơn" và "độ tự nhiên thấp hơn" clone chuyên nghiệp.
  - Việc tạo giọng làm trên Studio (web hoặc app). **Tài liệu API không có endpoint tạo giọng clone.**
  - Nguồn: [Nhân bản giọng](https://help.vbee.vn/huong-dan-su-dung/nhan-ban-giong-voice-cloning.md), [Cập nhật nhân bản nhanh 07/2025](https://help.vbee.vn/docs/thong-bao/ban-cap-nhat-thang-07-2025/cap-nhat-tinh-nang-nhan-ban-giong-noi-nhanh.md).
  - Chưa xác nhận: giọng clone có gọi được qua realtime không. Danh sách giọng realtime cho thấy là không.
- **Định dạng đầu ra.**
  - Batch: mp3 hoặc wav. Truyền pcm thì lỗi.
  - Realtime: mp3, wav hoặc pcm.
  - Tham số `sampleRate` nhận 8000, 16000, 22050, 24000, 32000, 44100 hoặc 48000, mặc định là mức cao nhất của giọng. `bitrate` nhận 8 đến 128 kbps.
- **Tốc độ.** `speed` từ 0.25 đến 1.9, mặc định 1.0. API **không có tham số pitch**. `emphasisIntensity` (0–100, bội số của 10) chỉ có ở batch và chỉ áp dụng cho một số giọng.
- **Độ dài tối đa mỗi request.** Realtime 300 ký tự, batch 100.000 ký tự ([Bản cập nhật 04/2026](https://help.vbee.vn/docs/thong-bao/ban-cap-nhat-thang-04-2026.md)).
- **SSML và ngắt nghỉ.** Tài liệu API **không nhắc đến SSML**. Batch có `clientPause` để chỉnh thời gian ngắt sau dấu phẩy, dấu chấm, dấu chấm phẩy và xuống dòng (0.1–10 giây). Studio có "thẻ Breaktime" (0–60 giây), nhưng cú pháp của thẻ và việc nó có chạy qua API hay không thì **chưa xác nhận** ([Thiết lập ngắt nghỉ](https://help.vbee.vn/huong-dan-su-dung/chuyen-van-ban/giao-dien-may-tinh/tinh-nang-ho-tro/thiet-lap-ngat-nghi.md)).
- **Chuẩn hóa số, đơn vị, từ viết tắt.** Tài liệu API không nói gì về việc này. Help center cho biết từ không có trong từ điển sẽ bị **đánh vần từng chữ cái**, ví dụ "VHVN" thành "vê hát vê nờ". Từ tiếng Anh cần được phiên âm kiểu Việt qua tính năng "Tùy chỉnh cách đọc" trên Studio. Nguồn: [Hướng dẫn viết cách đọc](https://help.vbee.vn/huong-dan-su-dung/chuyen-van-ban/giao-dien-may-tinh/tinh-nang-ho-tro/tuy-chinh-cach-doc/huong-dan-viet-cach-doc.md). Từ điển này có áp vào request API hay không thì **chưa xác nhận**. Rủi ro này đúng vào điểm yếu mình đang gặp: tên thương hiệu tiếng Anh và đơn vị đo.
- **Bằng chứng chất lượng.** Không có số liệu công khai nào (WER, MOS hay tỷ lệ rớt chữ) cho API. Changelog có các mục "Sửa lỗi chuyển văn bản giọng Ngọc Huyền, Anh Khôi bị thất bại" (3.162.0, ngày 2026-07-23) và "Sửa lỗi chuyển văn bản bằng API bị chậm dù với yêu cầu ngắn" (3.175.0, ngày 2026-10-02). Nguồn: [Changelog](https://help.vbee.vn/docs/changelog.md). Repo bên thứ ba `maycuatroi1/evo-dubbing` có benchmark MOS tiếng Việt, nhưng mẫu đã bị làm mù nên không tách được điểm riêng của Vbee.
- **Kiểm duyệt nội dung.** Studio chặn những văn bản bị coi là "không phù hợp" ([Văn bản không hợp lệ](https://help.vbee.vn/huong-dan-su-dung/chuyen-van-ban/giao-dien-may-tinh/khac-phuc-cac-loi-thuong-gap/van-ban-khong-hop-le.md)). Việc API có chặn giống vậy không thì **chưa xác nhận**. Đây là rủi ro với nội dung video ngắn.

### 3. STT

- **Ngôn ngữ.** Các trang [Batch](https://api-docs.vbee.vn/vbee-api/speech-to-text/batch-api.md), [Realtime](https://api-docs.vbee.vn/vbee-api/speech-to-text/realtime-api.md) và [Streaming](https://api-docs.vbee.vn/vbee-api/speech-to-text/streaming-api.md) đều **không có tham số ngôn ngữ**, và mọi ví dụ đều là tiếng Việt. Tính năng tự tạo phụ đề trên Studio chỉ cho chọn "Tiếng Việt/Tiếng Anh" ([Lồng tiếng Video](https://help.vbee.vn/huong-dan-su-dung/du-an/long-tieng-video.md)). Kết luận: **không có bằng chứng nào cho thấy hỗ trợ tiếng Trung**, và mọi dấu hiệu đều cho thấy STT chỉ dành cho tiếng Việt. Đây là suy luận từ tài liệu, chưa có câu nào nói thẳng "chỉ tiếng Việt".
- **Timestamp.** Chỉ có `utterances[]` gồm `text`, `startTime`, `endTime` tính bằng giây. **Không có timestamp mức từ hay ký tự.** Streaming có thêm `confidenceScore`, `startVoiceTime` và `endVoiceTime` cho mỗi câu.
- **Giới hạn.**
  - Batch: chỉ nhận WAV dưới 100 MB, gửi file (`audioContent`) hoặc URL (`audioUrl`), sample rate 8k đến 48k, phản hồi tối đa 4 giờ. Tài liệu không ghi giới hạn thời lượng. Với WAV 16 kHz mono 16-bit thì 100 MB tương đương khoảng 52 phút, nên một file 15 phút lọt.
  - Realtime: dưới 10 giây và dưới 10 MB.
  - Streaming: PCM 8k hoặc 16k mono, mỗi phiên tối đa 90 giây.
  - Nguồn: [Bản cập nhật 04/2026](https://help.vbee.vn/docs/thong-bao/ban-cap-nhat-thang-04-2026.md).
- **Batch và streaming.** Có cả ba kiểu. Batch nhận kết quả qua webhook (không bắt buộc) hoặc poll `GET /v1/stt/transcripts/:id`, khuyến nghị poll mỗi 2–5 giây ([Get transcript](https://api-docs.vbee.vn/vbee-api/speech-to-text/get-transcript.md)). Ví dụ curl trong trang này dùng `/api/v1/stt/...`, khác với URL ghi ở đầu trang. Đây là chỗ tài liệu tự mâu thuẫn.
- **Cách tính phí STT.** Tính theo giây, có trường `audioDurationSeconds` (làm tròn lên) "dùng để tính prepaid billing", và có lỗi `STT_INSUFFICIENT_SECONDS`. Đơn giá STT qua API **chưa công bố**. Trên Studio, `vbee-transcribe-flash` tốn 10 điểm/giây và `pro` tốn 15 điểm/giây; tôi chưa xác nhận API có tính giống vậy không.

### 4. Giới hạn, giá, điều khoản

- **Số luồng đồng thời.**
  - Gói trải nghiệm 05/2025 có 5 luồng xử lý đồng thời ([Bản cập nhật 05/2025](https://help.vbee.vn/docs/thong-bao/ban-cap-nhat-thang-05-2025.md)).
  - Từ 2026-01-30, số luồng tùy theo gói, và gói Tùy chỉnh (nay là VIP+) mua thêm được với giá 100.000đ/5 luồng ([Bản cập nhật 01/2026](https://help.vbee.vn/docs/thong-bao/ban-cap-nhat-thang-01-2026.md)).
  - Vượt số luồng thì nhận HTTP 429.
  - Tài liệu không ghi giới hạn số request mỗi phút (rate limit).
- **Giá.**
  - Từ 2026-01-30, bảng giá Studio và API hợp nhất. Gói miễn phí không có API. Các gói trả phí thường chỉ dùng được tối đa ½ quota cho API. VIP+ dùng được 100%.
  - Từ 2026-02-28, phí tính bằng điểm, 1 ký tự = 1 điểm ([Bản cập nhật 02/2026](https://help.vbee.vn/docs/thong-bao/ban-cap-nhat-thang-02-2026.md)).
  - Đơn giá công khai gần nhất là gói Tùy chỉnh **350đ/1.000 ký tự**, mua tối thiểu 5 triệu ký tự (khoảng 1,75 triệu đồng trước giảm giá), giảm 10–25% khi mua nhiều. Giọng quốc tế nâng cao tính x3 ([Bản cập nhật 05/2025](https://help.vbee.vn/docs/thong-bao/ban-cap-nhat-thang-05-2025.md)).
  - Một repo bên thứ ba ghi gói 599.000đ/năm cho 1,5 triệu ký tự, giá tại ngày 2026-07-25 (`maycuatroi1/evo-dubbing`, `benchmarks/tts-vi/providers.json`). Con số này chưa xác nhận.
  - Bảng giá hiện tại trên [vbee.vn/pricing](https://vbee.vn/pricing) được tải bằng JS. Tab API ghi "liên hệ để nhận báo giá tùy chỉnh".
  - **Ước tính:** một vòng lồng tiếng video 67 giây khoảng 1.000–1.500 ký tự, tức khoảng 0,35–0,5 nghìn đồng mỗi vòng, nhân đôi nếu dùng giọng clone nhanh. Chi phí không phải vấn đề.
- **Hợp đồng.** Có thể mua gói tự phục vụ. Tôi không thấy yêu cầu ký hợp đồng cho gói thường. Gói doanh nghiệp thì phải liên hệ.
- **Lưu trữ và quyền riêng tư.**
  - Audio TTS được giữ 3 ngày.
  - Điều khoản 3.1.6 ghi khách hàng **cấp phép cho Vbee dùng dữ liệu đầu vào và đầu ra "cho các mục đích cung cấp Dịch vụ, cải tiến, phát triển sản phẩm"**. Điều khoản theo luật Việt Nam ([Điều khoản sử dụng](https://policy.vbee.vn/chinh-sach-va-dieu-khoan/dieu-khoan-su-dung.md), [Chính sách bảo mật](https://policy.vbee.vn/chinh-sach-va-dieu-khoan/chinh-sach-bao-mat.md)).
  - Không có lựa chọn từ chối (opt-out) cho việc dùng dữ liệu để huấn luyện.

### 5. Fit với kiến trúc hiện tại

Interface đang có trong `services/localization/src/localization/providers/base.py`:

```python
class TtsProvider(Protocol):
    async def synthesize(self, text: str, voice: VoiceRef) -> bytes: ...
class SttProvider(Protocol):
    async def transcribe(self, audio: bytes, *, filename: str = ..., language: str | None = ...) -> TranscriptDocument: ...
```

**Adapter TTS realtime (nhỏ).**
- POST JSON với `mode=sync`, `outputFormat=wav`, `sampleRate` cố định, `speed`, rồi đọc toàn bộ body.
- Nếu `content-type` là JSON thì coi là lỗi. Lỗi 429 và 5xx là `TransientProviderError`. Lỗi 400 và 401 là `PermanentProviderError`.
- Duration lấy từ header WAV như hiện nay, vì API không trả duration.
- Đoạn ≤80 ký tự nằm gọn trong giới hạn 300.
- Ánh xạ `VoiceRef` sang `voiceCode`.
- Tài liệu không ghi WAV trả về là PCM 16-bit hay mono. Cần kiểm tra.

**Adapter TTS batch (vừa).** Batch là đường duy nhất dùng được giọng clone.
1. POST với `mode=async`, kèm `webhookUrl` bắt buộc. Có thể dùng một URL chỉ trả 2xx (một dự án bên thứ ba làm vậy rồi poll), nhưng việc này **chưa được Vbee xác nhận**.
2. Poll `GET /v1/tts/requests/{id}` đến khi `COMPLETED`.
3. Tải `audioLink` ngay, trước khi link hết hạn sau 3 phút.
4. Kiểm tra định dạng WAV.

Mỗi đoạn là một job riêng, nên độ trễ = thời gian hàng đợi + thời gian poll + thời gian tải. Có thể gộp nhiều đoạn vào một job rồi cắt bằng `clientPause.paragraphBreak`, nhưng làm vậy thì mất ranh giới từng đoạn và mình phải tự cắt lại audio.

**STT: không nên viết adapter.** API thiếu tham số ngôn ngữ, không trả `words`, chỉ nhận WAV, và `TranscriptDocument` sẽ không có `words`. Dùng để kiểm tra bản TTS tiếng Việt thì chỉ biết được câu đó có bị rớt chữ hay không, bằng cách so text, chứ không định vị được chữ nào bị rớt.

## Đánh giá rủi ro

| Rủi ro | Mức | Ghi chú |
|---|---|---|
| Giọng clone không chạy được qua realtime | Cao | Muốn giữ một giọng clone xuyên suốt video thì buộc phải dùng batch. |
| Độ trễ batch không có cam kết | Cao | Mục tiêu hiện tại là 30–50 s TTS mỗi vòng. Changelog 10/2026 vừa sửa lỗi API chậm. |
| Gói không bật realtime | Trung bình | Một bên thứ ba bị trả 400 `not supported in user package` ngày 2026-10-23 (nguồn yếu, nhưng cụ thể). |
| Đọc sai số, đơn vị, thương hiệu tiếng Anh | Trung bình đến cao | Từ không có trong từ điển bị đánh vần. Chưa rõ API có dùng từ điển tùy chỉnh không. |
| Chính sách giá thay đổi liên tục | Trung bình | Bảng giá đổi 4 lần trong 12 tháng (03/2025, 05/2025, 01/2026, 02/2026). |
| Vbee được dùng dữ liệu để cải tiến sản phẩm | Trung bình | Không có opt-out. Cần người dùng quyết. |
| Tài liệu không nhất quán | Thấp | Đường dẫn `/api/v1/stt`, chỗ ghi "Content Type" thiếu gạch nối, ví dụ callback lại lấy từ API legacy. |

## Nên thử gì trước (chưa chi tiền cho đến khi người dùng duyệt)

1. **Miễn phí, làm ngay:** nghe các file demo công khai trên S3 trong [Get list voices](https://api-docs.vbee.vn/vbee-api/voices/get-list-voices.md) (ví dụ `hn_female_ngochuyen_fast_news_48k-thg.mp3`) để xem giọng có hợp kiểu lồng tiếng video ngắn không. Không cần tài khoản.
2. **Gửi email hỏi Vbee** (contact@vbeeai.com) bốn câu hỏi ở mục bên dưới, trước khi mua gì.
3. **Nếu người dùng duyệt mua gói nhỏ nhất có API:** chạy realtime với `hn_female_ngochuyen_full_48k-fhg`, `outputFormat=wav`, trên khoảng 30 đoạn thật của mình (có số, đơn vị, thương hiệu tiếng Anh, câu dài 80 ký tự). Đo TTFB và tổng thời gian, chạy 5 luồng song song để xem khi nào gặp 429. Sau đó chạy STT nội bộ của mình (không dùng Vbee) để đếm số chữ bị rớt và so với VoiceStudio.
4. Chỉ thử batch với giọng clone nếu bước 3 đạt yêu cầu về chất lượng, và chỉ khi giữ một giọng xuyên suốt là bắt buộc.

## Câu hỏi còn mở

1. Gói trả phí nào bật TTS realtime (`mode=sync`)? Sắp tới realtime có hỗ trợ giọng clone (PERSONAL) hoặc giọng nam không?
2. Batch có cho bỏ `webhookUrl` và chỉ poll không? Độ trễ hàng đợi p50 và p95 với một job 80 ký tự là bao nhiêu?
3. STT có hỗ trợ tiếng Trung (cmn-CN) và có timestamp theo từ hay ký tự không? Có tham số ngôn ngữ nào chưa được ghi trong tài liệu không?
4. Từ điển "Tùy chỉnh cách đọc" và thẻ ngắt nghỉ của Studio có áp dụng cho request API không? Khi gặp số và đơn vị (ví dụ "5kg", "3,5%", "20h") thì engine đọc thế nào?
5. API có lọc nội dung giống Studio không? Nếu có, mã lỗi trả về là gì?
6. WAV trả về là PCM 16-bit mono hay định dạng khác?
7. Có thể tắt việc Vbee dùng dữ liệu đầu vào và đầu ra để cải tiến sản phẩm không?

## Giới hạn của nghiên cứu này

- Không gọi API nào. Mọi nhận định về độ trễ, chất lượng và chuẩn hóa văn bản đều dựa trên tài liệu, chưa đo thật.
- Bảng giá hiện hành trên vbee.vn/pricing render bằng JS và lấy dữ liệu từ API nội bộ, nên không đọc được số liệu giá mới nhất.
- Hai nguồn bên thứ ba trên GitHub (`maycuatroi1/evo-cli`, `nanxnguyen/remotion-ai-agentic-studio`) là bằng chứng sử dụng thực tế, nhưng không chính thức.
