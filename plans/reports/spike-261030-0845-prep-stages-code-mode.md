# Spike: gom 2a (transcribe) và 2b (detect_subtitles) thành tool của agent, chạy bằng CodeMode

Kết luận: **chạy được và song song thật, nhưng chậm hơn khoảng 3–4 giây, mất khả năng huỷ nhánh còn lại khi một nhánh lỗi, và workflow không nhận được transcript.** Không khuyến nghị.

Spike: `services/localization/spikes/g8_prep_code_mode.py`. Chạy lại bằng `./run.sh uv run python g8_prep_code_mode.py <direct|code|real:MODEL> <happy|fail>`. Hai stage được mô phỏng bằng activity có heartbeat (2a chạy 3 s, 2b chạy 2 s; ở kịch bản lỗi 2a chạy 6 s và 2b lỗi sau 0.5 s). `direct` có cùng hình dạng với `_prepare` hiện tại. Số liệu là một lần chạy mỗi ô, trừ khi ghi khác.

## M1. Độ trễ (kịch bản bình thường)

| Dạng | Stage đầu bắt đầu sau | Workflow xong sau |
|---|---|---|
| `direct` (hiện tại) | 0.2 s | 3.4 s |
| CodeMode, model giả lập (chỉ đo framework) | 0.4 s | 3.6 s |
| CodeMode, `openai/gpt-6-luna` thật, lần 1 | 3.1 s | 7.9 s |
| CodeMode, `openai/gpt-6-luna` thật, lần 2 | 1.9 s | 6.8 s |

- Hai stage vẫn chạy chồng nhau thật ở cả hai dạng (cùng bắt đầu, 2 activity riêng).
- Model thật viết code Monty đúng ngay lần đầu cả 2 lần (2 request model mỗi lần, không có retry).
- Chi phí thêm là một vòng LLM trước STT và một vòng sau để đọc kết quả: +3.4 đến +4.5 s trên video thật sẽ cộng vào mỗi video, và độ trễ của model dao động (xem dòng cuối M2: 11.8 s).

## M2. Một nhánh lỗi khi nhánh kia còn chạy

| Dạng | Lỗi tới run sau | Nhánh còn lại |
|---|---|---|
| `direct` | **0.8 s** | Bị huỷ (activity ghi nhận `cancelled`) |
| CodeMode, model giả lập | **6.8 s** | Chạy hết (6.4 s), không bị huỷ |
| CodeMode, model thật | 21 s tổng (model bắt đầu stage sau 11.8 s) | Chạy hết, không bị huỷ |

- `asyncio.gather` trong Monty đợi cả hai nhánh xong rồi mới báo lỗi, nên ta trả tiền cho stage chạy tiếp dù kết quả đã không dùng được.
- Model chỉ nhìn thấy `Exception: Activity task failed`; thông điệp gốc (`detect failed`) mất. Model thật không thử lại mà trả về một câu lỗi chung chung.

## M3. Dữ liệu đi đâu

- Transcript thử nghiệm 200 000 ký tự: ở dạng `direct`, workflow giữ nguyên nó (history 202 KB). Ở CodeMode, phần dài nhất workflow thấy chỉ 68–96 ký tự, tức **workflow không nhận được transcript**; nó ở lại trong sandbox, nên các bước sau (lồng tiếng, render) không lấy được nếu không có đường phụ qua object store.
- History tăng gấp đôi (432 KB so với 202 KB) vì kết quả tool vẫn được ghi vào history.

## Kết luận
Về mặt kiến trúc, 2a ‖ 2b không có quyết định nào cho model, nên chỉ thêm độ trễ (+3–4 s), mất huỷ nhánh khi lỗi, và buộc phải xây đường phụ cho dữ liệu. Giữ chúng trong workflow. CodeMode đáng dùng ở nơi model thật sự phán đoán (Dubber đo thời lượng rồi quyết định viết lại).

## Chưa kiểm
- Chỉ một lần chạy mỗi ô, nên chưa có độ lệch chuẩn; độ trễ model thật dao động nhiều (1.9 s đến 11.8 s ở stage đầu).
- Chưa kiểm `ingest` và bước đo tốc độ giọng; chưa kiểm hành vi regeneration (`_settle`) vì chúng nằm ngoài hai stage này.

## Thay đổi để lại (chưa commit)
`services/localization/spikes/g8_prep_code_mode.py` (mới). Khoá API được đọc từ `.env` vào biến môi trường cho lần chạy, không ghi ra file nào.
