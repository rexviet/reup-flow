# Brainstorm: lồng tiếng 2 voice theo người nói, Demucs và diarization chạy host

Ngày: 2026-11-10 · Branch: `feat/localization-dubber-code-mode` · Pipeline: `voicestudio`
Trạng thái: đã chốt hướng, chưa có plan, chưa có code (trừ `scripts/start-all.sh`).

## Outcome

Video có nhân vật nam và nữ được lồng tiếng bằng hai voice VoiceStudio khác nhau: câu của người nói nam dùng voice nam, câu của người nói nữ dùng voice nữ. Người review vẫn đổi voice từng cue được trong Studio. Demucs và diarization chạy trên host (M1 Max 64GB) thay vì trong Docker.

## Constraints

- Hiện chỉ có một `VoiceRef` cho cả lần chạy (`config.py:231`, `contracts/voice.py`); `DubSegment` và `Caption` chưa có `speaker`/`gender`. Provider TTS nhận voice theo từng lần gọi (`synthesize(text, voice)`), nên phần provider không phải sửa.
- `voice_id` phải nằm trong hash cache TTS, nếu không đổi voice vẫn dùng clip cũ.
- Secret chỉ nằm ở worker, không vào Temporal history. Token Hugging Face là secret mới, chỉ cần lúc tải model; sau đó chạy offline.
- Host worker cần identity MinIO riêng, quyền tối thiểu; không dùng lại identity của worker Docker.
- Docker Desktop trên Mac là VM Linux: không có GPU Metal, bind-mount chậm. Worker hiện ghim `torch==2.5.1` bản CPU.
- Workflow và activity phải replay được: giới tính/speaker là dữ liệu đầu ra của activity, không đọc env trong workflow.
- Vbee pipeline không đổi output.

## Non-goals

- Một segment STT có nhiều người nói cùng lúc: tạm không tách; lấy người nói chiếm nhiều thời gian nhất.
- Pipeline `vbee`.
- Đổi encoder render ffmpeg sang VideoToolbox (chưa kiểm tra encoder đang dùng, chưa có bằng chứng cần đổi).
- Chuyển worker LLM/STT/TTS ra host: chúng chỉ gọi HTTP, chạy host không nhanh hơn.
- Diarization bằng `sherpa-onnx`: đã cân nhắc, không chọn.

## Acceptance criteria

- Video thử có một nam một nữ: câu của mỗi người dùng đúng voice cấu hình, kiểm tra bằng nghe và bằng `speaker`/`voice_id` trong segment.
- Video một người nói hoặc voiceover: hành vi như cũ, dùng voice mặc định; có công tắc tắt diarization.
- Đổi voice một cue trong Studio rồi revoice: chỉ cue đó tạo clip mới.
- Demucs chạy ở host worker, kết quả (vocals, background) dùng được bởi các bước sau; nếu MPS lỗi hoặc cho kết quả lệch CPU thì có đường lui về CPU trên host hoặc về worker Docker.
- `scripts/start-all.sh start|status|stop` bật và dừng đủ Docker, bridge, watcher và host worker, không để lại process mồ côi.
- Test mặc định (không live) pass; test live cần Studio/VoiceStudio/host worker đánh dấu riêng.

## Các hướng đã so sánh

| Hướng | Phụ thuộc nhiều nhất vào | Hỏng trước khi |
|---|---|---|
| 1. Gán voice thủ công từng cue trong Studio | Người review có thời gian sửa | Khối lượng video lớn, sửa tay không kịp |
| 2. LLM (Localizer) gắn `speaker`/`gender` từ văn bản | Ngữ cảnh văn bản đủ để đoán giới tính | Thoại ngắn, mơ hồ, tên không rõ giới |
| 3. Diarization từ audio vocals (pyannote) | Vocals tách sạch và pyannote chạy ổn trên arm64 | Thoại dưới 1 giây, hai người nói chồng nhau, nhạc nền còn sót |

**Chọn:** hướng 3 làm đường chính, giữ hướng 1 làm lớp sửa tay. Hướng 2 không làm lúc này.

## Trade-offs

- **Host worker thay vì Docker:** nhanh hơn (có MPS cho Demucs, không qua VM), đổi lại thêm một thành phần phải quản lý ngoài compose, cần identity MinIO riêng và `start-all.sh` phải quản lý vòng đời. Hỏng trước khi: Demucs trên MPS lỗi op hoặc lệch kết quả. Rẻ nhất để bỏ: task queue riêng nên chuyển lại về Docker bằng cách đổi queue.
- **pyannote thay vì sherpa-onnx:** chính xác hơn, đổi lại cần token HF (miễn phí, một lần) và phụ thuộc torch trên host. Hỏng trước khi: model gated bị đổi điều khoản hoặc token hết hạn lúc cài máy mới.
- **Người chiếm nhiều thời gian nhất cho segment lẫn nhiều người:** đơn giản, đổi lại câu có hai người sẽ dùng một voice. Chấp nhận vì đã có chỉnh tay.

Better approaches: none — recommended direction is the requested one (host worker + pyannote đã được chọn sau khi so sánh ba hướng; không tìm thấy hướng nào rẻ hơn mà đạt acceptance).

## Seam và tính mở rộng

Biến đổi có bằng chứng: voice theo từng segment (đã có hai voice, nam và nữ) và nơi chạy (Docker hay host). Giữ cụ thể: map `gender → voice_id` là hai biến cấu hình, chưa làm registry nhiều voice. Task queue riêng là seam duy nhất thêm vào, vì đã có hai nơi chạy thật.

## Đã làm

- `scripts/start-all.sh` (start/stop/status; Docker có profile `review`, rồi watcher host). Mới kiểm tra cú pháp bằng `bash -n` và `--help`, **chưa chạy thật**. Chưa quản lý host worker vì nó chưa tồn tại.

## Handoff

Sang `ak-plan` rồi `/ak-cook`. Thứ tự gợi ý cho plan: (1) spike Demucs trên MPS và pyannote trên arm64 để đo; (2) host worker + task queue + identity MinIO + mở rộng `start-all.sh`; (3) contract `speaker`/`gender`/`voice_id` theo segment và hash cache TTS; (4) map giới tính sang voice, công tắc tắt; (5) Studio hiển thị và đổi voice từng cue; (6) test và tài liệu.

## Câu hỏi còn mở

- `voice_id` của voice nam và voice nữ trong VoiceStudio là gì? (cần để đặt cấu hình)
- Demucs có chạy ổn và cho kết quả tương đương trên MPS không? Chưa đo; spike quyết định.
- Cách đoán giới tính của từng speaker: ước lượng pitch hay classifier nhỏ? Chưa chọn; spike cùng với diarization.
- Khi có một người nói, diarization tắt tự động hay chỉ khi cấu hình? Chưa chốt.
