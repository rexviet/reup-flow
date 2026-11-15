# CodeMode cho Localization: các spike và lần chạy thật (30/10/2026)

Kết luận: Dubber chế độ `code` đã chạy thật ở 3 video, không có activity nào lỗi, và còn một điểm cần cải thiện (model gọi từng lần một và lặp gần như cùng một câu). CodeMode không đáng dùng cho bước chuẩn bị 2a ‖ 2b, và chưa cần cho phần sửa lỗi của Localizer. Chưa so sánh được Dubber `code` với `single`.

Nhánh: `feat/localization-dubber-code-mode`, chưa commit. Trang tổng hợp có hình: https://claude.ai/artifact/DSxBPkqnbk5kZTCTbUcPJx

## 1. Các spike (không đụng pipeline thật)

| Spike | File | Kết quả |
|---|---|---|
| G7: Dubber gọi tool qua CodeMode trên Temporal | `services/localization/spikes/g7_code_mode.py` | Mỗi lời gọi lồng là một activity trên đúng queue; `gather` chạy chồng nhau thật (overlap 3); sau SIGKILL các lời gọi đã xong không chạy lại. Guard "3 lần" đếm theo lịch sử hội thoại mù với lời gọi từ code, guard đặt trong tool thì chạy đúng |
| G8: 2a ‖ 2b thành tool của agent | `services/localization/spikes/g8_prep_code_mode.py` | Chậm hơn 3.4–4.5 s; khi một nhánh lỗi phải đợi nhánh kia chạy hết (6.8 s so với 0.8 s) và nhánh kia không bị huỷ; workflow không nhận được transcript. Không làm |
| Leo thang sửa lỗi của Localizer | `services/localization/eval/escalation_spike.py` | Slot gốc: sửa 2 lần còn 9 vấn đề trên 66 (3.6 s), CodeMode còn 16 (15.1 s, 7 trên 40 ca lỗi). Slot ép 0.7: sửa 2 lần còn 30 trên 96, CodeMode còn 20 (14.3 s). Chưa cần CodeMode ở slot thường |
| Ước lượng âm tiết so với giọng đọc thật | `eval/estimate_vs_real_extract.py`, `estimate_vs_real_compare.py`, `measure_fit_miss_rate.py` | Giọng thật dài hơn ước lượng khoảng 1.2 lần (trung vị); lần đo thật đầu tiên báo 13.7% dòng quá dài khi tính mọi lần chạy, 6.7% khi tính mỗi video một lần (cận trên). Chưa đủ lý do để gộp Localizer và Dubber |

Hạn chế chung: mỗi cấu hình chỉ chạy 1–2 lần, kết quả dao động, chất lượng câu dịch chưa được chấm.

## 2. Triển khai Dubber `code`

Sub-agent triển khai báo (báo cáo riêng: `fullstack-developer-261030-0809-dubber-code-mode.md`): chế độ thứ ba `dubber_mode="code"`, mặc định vẫn `single`; guard `SpokenRewrites` đếm ở phía workflow; patch `dubber-code-mode`; 10 history cũ vẫn replay; toàn bộ test của service đạt 2981 passed, 0 failed; `pydantic-ai-harness 0.61.0` chạy cùng `pydantic-ai-slim 2.61.0` nên không phải nâng slim.

## 3. Chạy thật 10 video (giọng VoiceStudio trên 4090, `DUBBER_MODE=code`)

| Video | Gen | Thời gian | Kết quả | Dòng thoại | Dubber |
|---|---|---|---|---|---|
| 7518 | g2 | 64 s | publish | 0 | không gọi |
| 7526 | g2 | 63 s | publish | 0 | không gọi |
| 7552 | g2 | 58 s | needs_human | 2 | không gọi |
| 7565 | g2 | 70 s | needs_human | 2 | không gọi |
| 7574 | g2 | 32 s | publish | 0 | không gọi |
| 7575 | g16 | 75 s | needs_human | 3 | chạy |
| 7609 | g2 | 41 s | publish | 0 | không gọi |
| 7652 | g2 | 183 s | needs_human | 10 | chạy (2 lần) |
| 7664 | g2 | 82 s | publish | 3 | không gọi |
| 7665 | g6 | 123 s | publish | 2 | chạy |

Đã chạy trước đó: 7616 g10 (254 s) và 7618 g10 (252 s), đều publish, Dubber không được gọi (lần chạy nguội sau khi bật GPU).

Lý do `needs_human`:
- 7552, 7565, 7652: nguồn là **tiếng Anh** (lời bài hát và "Thank you"). Pipeline dịch từ tiếng Trung, reviewer báo dòng dịch không mang các từ của nguồn. Không liên quan Dubber. Mình chưa kiểm xem workflow có cơ chế nhận diện ngôn ngữ nguồn hoặc video nhạc có lời hay không.
- 7575: lỗi của Dubber. Dòng 2 có slot 1000 ms, sau viết lại vẫn đọc mất 1744 ms ở mức tăng tốc 1.15.

Quan sát về Dubber `code` ở 7575 (lịch sử Temporal của `video-7575765838708520689-g16`):
- Model dùng `run_code` mỗi lần một lời gọi (`get_segment`, rồi `synthesize` ba lần, rồi `accept`), qua 6 lượt model, không dùng `asyncio.gather` vì chỉ có một dòng cần sửa.
- Ba lần viết lại gần như giống nhau (khác dấu câu cuối): 1744, 1752 và 1744 ms. Ba lần TTS gần như bị lãng phí.
- Hướng sửa có thể thử: nêu rõ trong hướng dẫn để model thử vài phiên bản ngắn dần trong một lượt `gather`. Giới hạn 3 lần viết lại tính từng lần `synthesize`, nên một lượt như vậy dùng hết ngân sách của dòng đó.

## 4. Chưa làm

- So sánh `single` với `code` trên 7575, 7652 và 7665: khi chuyển sang `single` để chạy thì VoiceStudio trả 530 (tunnel không tới được máy GPU). Đã trả `DUBBER_MODE` về `code`; không có dòng outbox nào bị đẩy dở.
- Chưa nghe hay xem các video, nên chưa đánh giá chất lượng giọng và lời dịch.
- Chưa thêm `ConversationSizeGuard` cho Dubber `code`; chưa kiểm kill worker thật.
- Mutation harness chưa chạy (theo quy ước, một lần sau phase 10).

## 5. Việc dọn dẹp và vận hành

- `data/review/` chỉ còn `7616…-g6-vbee.mp4`, `7618…-g6-vbee.mp4` và các video của lần chạy này. 418 MB file cũ nằm ở `data/review-removed-261030/` (gitignore), xóa hẳn khi bạn đồng ý.
- Sao lưu: `data/backups/` có outbox (09:43, 09:59) và `.env` (trước khi đổi `DUBBER_MODE`).
- Bản render lấy từ `render_key` trong history; khoá `output/` đã publish không đọc được bằng role của worker.
- Script batch đầu tiên có lỗi (`UPDATE ... RETURNING` in thêm dòng `UPDATE 1` làm sai tên workflow); đã sửa và thêm bước đợi không còn workflow nào đang chạy.

## Câu hỏi còn mở

1. Bật lại VoiceStudio để chạy so sánh `single` với `code` trên 3 video có Dubber?
2. Sửa hướng dẫn của Dubber `code` để thử nhiều phiên bản ngắn hơn trong một lượt `gather`?
3. Thêm `ConversationSizeGuard` cho Dubber `code`; chấp nhận các gói phụ của harness trong image?
4. Làm vòng sửa thứ hai cho Localizer (cần `workflow.patched` và history replay mới)?
5. Commit nhánh `feat/localization-dubber-code-mode` chưa?
