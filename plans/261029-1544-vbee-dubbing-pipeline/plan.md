# Pipeline lồng tiếng bằng Vbee (không cần Colab)

Trạng thái: đang làm phase 1–3 (2026-10-29).

Người dùng đã duyệt kế hoạch và chọn:
- **Pipeline cũ:** giữ lại làm dự phòng. Starter chọn chạy workflow nào theo một setting.
- **Thứ tự làm:** phase 1–3 do 3 sub-agent làm song song, sau đó đến phase 4, rồi phase 5.
- **Contract chung giữa các bước:** `src/localization/contracts/vbee_flow.py`, gồm `Utterance`, `SpeechTranscript`, `Slot`, `SlotPlan`.

## Bối cảnh

- Spike ở `plans/reports/fullstack-developer-261029-1437-vbee-dub-spike.md` đã làm ra 7616 và 7618 bằng chuỗi: Vbee STT (tự dịch sang tiếng Việt) → dò quãng ngừng → chia chữ vào slot → Vbee TTS → mix → render ffmpeg.
- Người dùng xem bản v2 và đánh giá: "ngoài câu 'đầu lợn…' thì khá ổn, lỗi nhỏ chấp nhận được".
- Người dùng chốt đổi pipeline theo hướng này và không dùng thêm dịch vụ STT nào khác.

## Mục tiêu và ràng buộc

- **Mục tiêu:** pipeline chạy hết một video mà không cần Colab. Thời gian nhanh hơn pipeline hiện tại (232–288 s cho 7616 và 7618).
- **Ràng buộc:**
  - Chỉ dùng Vbee cho STT và TTS. Giọng đọc lấy từ `VBEE_VOICE_ID` (giọng clone, chỉ chạy được qua batch).
  - Secret (`VBEE_APP_ID`, `VBEE_API_TOKEN`, `VBEE_VOICE_ID`) không bao giờ được in hay ghi ra.
  - Workflow cũ cùng các history replay của nó không bị đổi.
- **Không làm:**
  - Không sửa dịch khi không có câu gốc tiếng Trung.
  - Không thêm dịch vụ STT nào khác.
  - Không có review nghĩa câu dịch (Jev): không còn câu gốc để so.

## Bài học từ spike đưa vào thiết kế

1. Chia chữ vào slot bằng **code**, theo thời lượng giọng nói rồi dời tới dấu câu gần nhất. Spike v2 làm được, còn LLM thì trôi nội dung và bỏ mất 20 s cuối.
2. LLM chỉ rút gọn **từng slot bị dài**, trên đoạn chữ đã chia sẵn. Code kiểm lại kết quả.
3. Dò quãng ngừng ở -30 dB, khoảng ngừng tối thiểu 0,15 s, rồi gộp thành slot 3–10 s bằng quy hoạch động.
4. Vbee TTS chạy batch: webhook giả, tự poll, tải link (có redirect) ngay trong 3 phút, chạy song song khoảng 3.
5. Tốc độ đọc đo trên câu thật, khoảng 6 âm tiết/s; không đo trên câu mẫu.

## Các phase

| # | Việc | Có đổi workflow không |
|---|---|---|
| 1 | Provider `VbeeStt` (async + poll) và `VbeeTts` (batch + poll + tải audio): phân loại lỗi tạm thời/vĩnh viễn, settings, test với HTTP server giả | Không |
| 2 | Activity `plan_slots`: dò giọng nói, bỏ qua video không có lời, gộp slot; gán chữ của các utterance vào slot bằng code | Không |
| 3 | Activity rút gọn: ước lượng thời lượng, gọi LLM một lần cho các slot quá dài, code kiểm lại (dữ liệu không tin cậy vẫn bọc như cũ) | Không |
| 4 | Workflow mới `VbeeDubbingWorkflow` (xem bên dưới) | Thêm workflow mới |
| 5 | Bật chạy: biến môi trường trong compose, starter chọn workflow theo setting, preflight kiểm Vbee thay cho VoiceStudio; chạy thật 7616, 7618 và 3 video khác | Không |

**Chuỗi bước của workflow mới (phase 4):**
1. Tách giọng bằng demucs (chạy trên máy), song song với dò phụ đề gốc.
2. Vbee STT.
3. `plan_slots`.
4. Rút gọn các slot quá dài.
5. Vbee TTS, chia đều batch.
6. Mix audio và dàn phụ đề.
7. Review bằng code: timing, phụ đề, chữ gốc còn sót. Không đạt thì rút gọn lại đúng các slot lỗi, một lần.
8. Render một lần, rồi publish.

Đây là một workflow type riêng, không gắn vào workflow cũ bằng `workflow.patched`, nên các history cũ giữ nguyên. Workflow mới có bộ history replay riêng.

## Nghiệm thu

- 7616 và 7618 chạy hết pipeline thật mà không cần Colab.
- Thời gian của mỗi video thấp hơn pipeline hiện tại.
- Người dùng xem video và chấp nhận được.
- Test, ruff và replay đều xanh.

## Rủi ro đã biết

- Việc Vbee STT tự dịch không có trong tài liệu của Vbee, nên Vbee có thể đổi bất cứ lúc nào.
- Có lỗi dịch không sửa được, ví dụ "Đầu lợn…".
- Mốc thời gian chỉ ở mức utterance, nên lời có thể lệch hình vài giây.
- Batch TTS không có cam kết về độ trễ.
- Vbee được dùng input và output để cải tiến sản phẩm.
