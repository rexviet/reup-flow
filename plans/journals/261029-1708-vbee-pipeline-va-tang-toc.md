# Bỏ Remotion, nghe lại video, rồi đổi sang pipeline Vbee

**Date**: 2026-10-29 17:08
**Severity**: High
**Component**: services/localization (render, TTS, review, workflow Vbee mới)
**Status**: Ongoing. Chưa có lần nào được commit. Phase 5 (chạy e2e thật bằng Vbee) chưa làm.

## Tóm tắt

Hôm nay có ba mạch việc. Buổi sáng chốt nốt các follow-up eval E5/E6. Sau đó tôi cắt thời gian mỗi video từ khoảng 12–13 phút xuống còn 4–5 phút bằng cách bỏ Remotion. Khi người dùng nghe lại video, họ phát hiện pipeline kiểm tra quá chặt và có một lỗi giọng thật: TTS nuốt chữ. Cuối ngày, người dùng chọn đổi sang pipeline Vbee, giữ pipeline cũ làm đường lui. Phase 1–4 đã xong trên code. Phase 5 và việc commit chưa làm.

## Diễn biến chính

- **E5/E6.**
  - Check glossary giờ chỉ chấp nhận tên riêng.
  - `PROMPT_VERSION` được nâng lên `localizer-2`.
  - E6 đã kiểm trên run thật: 7616 và 7618 không còn lỗi `TextTooLong`. Hai video có 7 và 11 segment, dòng dài nhất lần lượt 364 và 336 byte.
  - AC7 đã kiểm trên 7616 g3: trace của run có điểm của reviewer.
- **Tốc độ.** Người dùng nói thẳng: "12 phút cho 1 video là không chấp nhận được".
  - Remotion mở Chrome và vẽ 2.000 khung hình, mất khoảng 217 s mỗi lần render. Phần render chiếm khoảng 60% tổng thời gian.
  - ffmpeg dựng cùng nội dung trong một lượt chỉ mất 8,9 s. Số này đo lại trên máy rảnh.
  - Remotion và `worker-render` bị bỏ, render chuyển vào `worker-media`. Batch TTS được chia đều cho 2 slot song song.

| Video | Trước | Sau khi sửa render + TTS | Cuối ngày (GPU đã warm) |
|---|---|---|---|
| 7616 | 710 s | 331 s | 232 s |
| 7618 | 786 s | 271 s | 288 s |

- **Nghe lại video.**
  - Check chữ gốc còn sót bắt nhầm chính phụ đề tiếng Việt của mình, vì OCR chạy trên bản đã render. Check này được làm lại để dùng box phụ đề gốc.
  - Ngưỡng đọc nhanh của review là 1,12 nhưng của dubber là 1,15. Hai ngưỡng được gộp về 1,15.
  - TTS omnivoice nuốt chữ, ví dụ "bảy mươi" và "sáu miligam sắt". Lỗi này đã được xác nhận bằng cách chạy STT ngược lại trên audio TTS.
  - STT tách "3 0岁" và cắt "4 . 6" sang hai segment khác nhau.
  - Các bản sửa: nối lại chữ số bị tách; TTS từng câu ngắn; cache key có version theo `CLIP_METHOD`; review chạy trước render, nên mỗi video chỉ render một lần.
- **Khám phá Vbee.**
  - Nghiên cứu tài liệu nói STT của Vbee chỉ hỗ trợ tiếng Việt và chỉ có timestamp theo utterance.
  - Spike thật cho thấy với audio tiếng Trung, Vbee trả về một bản dịch tiếng Việt trôi chảy. Tài liệu không nhắc gì đến hành vi này.
  - Cả clip 67 s chỉ ra 3 utterance. Vbee nghe được câu "146 loại báo cáo…", câu mà whisperx đã lặng lẽ bỏ mất.
  - Dò quãng ngừng trên vocals ở -30 dB / 0,15 s cho mốc cắt mỗi 1,5–3 s.
  - Dub spike v1/v2: khi LLM chia câu, chữ bị trôi khỏi đúng chỗ và mất 20 s cuối của 7618. Cắt bằng code tại dấu câu thì chạy được.
  - Người dùng đánh giá v2 "khá ổn", trừ câu "Đầu lợn…".
- **Xây pipeline Vbee.**
  - Controller viết contract dùng chung `contracts/vbee_flow.py` trước.
  - Sau đó phase 1–3 chạy song song: providers, `plan_slots`, slot shortener.
  - Phase 4 thêm `VbeeDubbingWorkflow`, chọn qua `LOCALIZATION_PIPELINE` (mặc định `voicestudio`).
  - Voice id được giữ ngoài history bằng một marker.
  - Theo agent, cả bộ test tăng 1401 → 1509 → 1628 → 1721 passed. Controller đang kiểm lại.

## Quyết định và lý do

- **Bỏ Remotion, render bằng ffmpeg.** Nội dung cần dựng chỉ gồm video gốc, một khung che cố định và các dòng phụ đề tĩnh. Không có lý do gì để mở một trình duyệt và vẽ từng khung hình. Đây là thay đổi lớn nhất về thời gian.
- **Review trước render.** Review trước render thì mỗi video chỉ render một lần. Nó cũng bỏ được vòng lặp vô lý là OCR đọc chính phụ đề mình vừa vẽ.
- **TTS từng câu ngắn, không dùng cả đoạn dài.** omnivoice nuốt chữ khi câu dài. Câu ngắn tốn thêm request nhưng đọc đủ chữ. Cache key có version theo `CLIP_METHOD` để không dùng lại clip cũ bị nuốt chữ.
- **Đổi sang Vbee, không thêm dịch vụ STT nào khác, giữ pipeline cũ.** Ban đầu tôi đóng khung vấn đề là "Vbee không thay được whisperx". Người dùng bác cách nhìn đó. Họ muốn đổi cả pipeline cho hợp với Vbee: lấy mốc thời gian từ quãng ngừng trong vocals, và bỏ cuộc tranh luận về các check. Họ đúng. Một workflow type riêng giữ nguyên history cũ, và `LOCALIZATION_PIPELINE=voicestudio` vẫn là mặc định, nên chưa có gì thay đổi khi chưa đổi `.env`.
- **Chia chữ vào slot bằng code, không dùng LLM.** LLM vừa chia câu vừa rút gọn trên một utterance 60 s thì trôi và bỏ mất phần cuối. Nó còn bỏ cả câu mà không ghi lại trong `fixes`. Nếu cần LLM thì chỉ cho nó làm từng slot một, trên đoạn chữ đã chia sẵn, và kiểm tra lại bằng code.

## Sai lầm và bài học

- **Đo benchmark khi workflow đang chạy.** Người dùng bắt được việc tôi đo tốc độ trong lúc một workflow khác đang tranh CPU. Số đo như vậy vô giá trị. Tôi đã dừng và đo lại trên máy rảnh. Bài học: trước khi đo, kiểm tra máy đang rảnh, và ghi điều kiện đo cạnh con số.
- **Đổ lỗi sai cho Colab.** Tôi khẳng định quãng kẹt 70 s là do Colab đổi model giữa STT và TTS, rồi phải rút lại. Người dùng tìm ra nguyên nhân thật: VoiceStudio nạp weights lên CUDA ở lần dùng đầu tiên, nên họ phải warm thủ công. Bài học: không gọi tên nguyên nhân khi chưa có log hay timing chứng minh.
- **Chỉ restart một nửa hệ thống khi URL Colab đổi.** Tôi chỉ restart `worker-agents`. Starter cũng gọi VoiceStudio nhưng vẫn giữ URL cũ, nên một video báo "running" đã nằm 9 phút mà không được dispatch. Tôi đã lưu điều này thành memory. Bài học: khi một endpoint đổi, liệt kê mọi service đọc biến đó (`rg` trong compose) rồi restart hết.
- **Pipeline kiểm tra quá chặt nhưng vẫn bỏ sót lỗi thật.** Các check bắt nhầm chính phụ đề của mình và dùng ngưỡng lệch nhau, trong khi lỗi TTS nuốt chữ thì lọt qua tới tai người dùng. Bài học: test xanh chưa phải bằng chứng. Phải nghe video, và kiểm audio TTS bằng STT ngược lại.
- **Tin tài liệu hơn spike.** Tài liệu Vbee nói STT chỉ hỗ trợ tiếng Việt. Thực tế nó dịch luôn từ tiếng Trung. Chỉ gọi API thật mới lộ ra điều này.

## Rủi ro còn lại

- Việc Vbee STT tự dịch sang tiếng Việt không có trong tài liệu. Vbee có thể đổi hoặc tắt hành vi này bất cứ lúc nào, và mình không có tham số ngôn ngữ hay glossary để điều khiển.
- Lỗi dịch không sửa được vì không còn câu gốc tiếng Trung, ví dụ "Đầu lợn càng ngày càng xinh đẹp" và "Mandi". Pipeline mới cũng không có review nghĩa (Jev).
- Điều khoản 3.1.6 cho Vbee dùng dữ liệu đầu vào và đầu ra để cải tiến sản phẩm, và không có opt-out.
- Mỗi lần retry STT hoặc TTS đều tốn tiền, tính theo ký tự. Retry của Temporal cần giới hạn rõ.
- Mốc thời gian chỉ ở mức utterance, nên lời có thể lệch hình vài giây. Đoạn hát cũng không được phân biệt với lời nói.
- Batch TTS không có cam kết về độ trễ.
- Con số 1721 test chưa được controller kiểm lại độc lập.

## Việc tiếp theo

1. Controller hoàn tất việc kiểm lại cả bộ test, ruff và replay của phase 4.
2. Phase 5: chạy 7616 và 7618 thật với `LOCALIZATION_PIPELINE=vbee`, không dùng Colab. So thời gian với 232 s và 288 s, rồi đưa người dùng nghe.
3. Sửa mutation harness: bỏ các tham chiếu tới renderer cũ và các target `phase-08` đã lỗi thời. Chạy harness theo quy ước là chạy một lần ở cuối.
4. Hỏi Vbee liệu việc tự dịch có chủ đích không, có tắt được không, và có opt-out điều khoản dữ liệu không.
5. Chia commit theo từng mạch: render ffmpeg, TTS, review, các bản sửa số, Vbee providers, rồi workflow. Không commit `.env`.

## Cập nhật tối 2026-10-29: Vbee chạy thật, bị loại vì giá; chuyển sang GPU thuê

### Diễn biến

- **Vbee chạy thật, không cần Colab.** Bộ test kiểm lại cho đúng 1721 pass.
  - 7616 xong trong 168 s, `needs_human` vì check chữ gốc báo nhầm.
  - 7618 xong trong 274 s và được publish thẳng. Người dùng chấm chất lượng cả hai "rất ổn".
- **Chi phí Vbee đối chiếu với dashboard.**
  - Batch STT tính 17 điểm/giây; dashboard ghi 1.139 và 1.428 điểm, khớp đúng số tôi tính.
  - TTS giọng clone tính 2 điểm/ký tự.
  - Một điểm ≈ 1,33đ (167.000đ cho 125.500 điểm), nên mỗi video ≈ 6.000–7.300đ, tức khoảng 5.100đ mỗi phút video.
  - Người dùng kết luận: quá đắt để sản xuất hàng loạt.
- **Chuyển sang VoiceStudio trên RTX 4090 thuê ở Vast.ai.**
  - Dùng image `ghcr.io/debpalash/voicestudio:stable`, port 3900, tunnel Cloudflare chạy trong on-start script, `OMNIVOICE_IDLE_TIMEOUT_S=3600`.
  - Bốn trục trặc khi dựng:
    - lúc đầu engine STT đang là faster-whisper chứ không phải whisperx;
    - sau đó server báo chưa có model STT (HTTP 409 `asr_model_missing`);
    - người dùng tải Whisper large-v3 xong thì engine lại tự quay về faster-whisper;
    - cuối cùng chốt dùng `STT_MODEL=whisper-1`, tức "engine đang bật".
  - faster-whisper tốt hơn whisperx ở hai điểm: có dấu câu và ghép số đúng ("30岁", "4.6").
- **Đo trên 4090.**
  - STT 8–11 s; trên T4 là 49 s.
  - TTS 17–40 s mỗi vòng khi GPU đã nóng. Lần chạy đầu sau khi bật máy mất 85 s.
  - 7618 g8 xong 199 s, g9 xong 221 s, cả hai published. 7616 g9 xong 194 s.
  - Chi phí ≈ 240–440đ mỗi video (GPU $0,405/giờ cộng LLM), rẻ hơn Vbee 15–30 lần.
- **Giọng clone mới trên server 4090 bị lỗi.** Gần như mọi đoạn TTS đều có thêm cụm "Tủ lạnh / Tô lạnh" ở đầu.
  - STT nghe lại từng clip cho thấy lỗi nằm ở voice profile: transcript của audio mẫu không khớp.
  - Người dùng sửa voice profile; nghe lại 11/11 clip thì hết lỗi.
- **Ba bản sửa cuối cùng, toàn bộ suite 2937 pass.**
  - Check số dùng chung một module `text/vi_numbers.py`, so theo giá trị. Chấp nhận "hai mươi", "bốn phẩy sáu", "đôi mươi", "năm chục"; không chấp nhận "Ngũ", "tá".
  - faster-whisper trả "4." thành một word; nay được nối với "6" nên số không bị cắt giữa hai đoạn.
  - Check chữ gốc chỉ báo khi ô phụ đề nằm ngoài vùng che liên tục ≥ 2 s. Lý do: OCR lệch 70–90 px xen kẽ suốt video.
- **Sơ đồ kiến trúc vẽ lại** tại https://claude.ai/artifact/AERLctbPRNFbUJ5cfhHoFr, không còn Node render, `VbeeDubbingWorkflow` và starter.

### Quyết định

- Pipeline chính quay về `voicestudio` chạy trên GPU thuê. Vbee giữ làm đường dự phòng; trong `.env` hiện là `LOCALIZATION_PIPELINE=voicestudio`.
- TTS vẫn do code gọi, không thành tool của Dubber Agent. Người dùng hỏi về việc này nhưng chưa chốt.
- Đánh đổi đã nói rõ với người dùng: vùng che hụt khoảng 40 px không còn bị bắt, vì OCR quá nhiễu.

### Sai lầm và bài học

- Tôi gửi 7618 ngay sau khi 7616 fail vì 409, nên tốn thêm một lượt chạy hỏng. Bài học: video đầu chưa qua bước đầu tiên thì không gửi video thứ hai.
- Tôi kiểm giọng clone bằng `VOICE_ID` lấy trong `worker-agents`, nhưng container này không có biến đó, nên lần thử đầu đọc bằng giọng mặc định. Bài học: kiểm biến môi trường có thật trong container trước khi dùng làm bằng chứng.
- Lúc đầu nghi STT gây ra chất lượng kém. Cho STT nghe lại từng clip TTS mới chỉ ra đúng chỗ hỏng là giọng clone. Đây là phép kiểm rẻ và nhanh nhất cho lỗi âm thanh.
