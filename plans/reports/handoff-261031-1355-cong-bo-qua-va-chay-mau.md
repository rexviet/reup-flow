# Handoff: video localization, phiên 2026-10-30 → 2026-10-31

Branch `feat/localization-dubber-code-mode` (từ commit `d7040eb`), chưa push. **132 file chưa commit**, gồm Dubber CodeMode, trace Langfuse, sửa che mặt, cổng quảng cáo, cổng tiếng nói, spike và script eval. Chưa có commit nào trong phiên này vì người dùng chưa yêu cầu.

Hãy đọc file này trước, rồi đọc:
- journal `plans/journals/261031-1355-cong-bo-qua-va-chay-mau-8-video.md`;
- các báo cáo trong `plans/reports/`: `fullstack-developer-261030-0809-dubber-code-mode.md`, `fullstack-developer-261030-1050-langfuse-agent-traces.md`, `fullstack-developer-261030-1130-skipped-ad-gate.md`, `fullstack-developer-261031-1025-no-voice-shortcut.md`, cùng `spike-*` và `experiment-261030-1033-*`;
- handoff trước `handoff-261030-0744-video-localization-session.md` (cấu hình, cách chạy video, quy tắc người dùng vẫn đúng, trừ chỗ về Vast.ai đã sửa).

Link tham chiếu (private, chỉ chủ tài khoản mở được):
- Sơ đồ kiến trúc theo flow mới: https://claude.ai/artifact/AERLctbPRNFbUJ5cfhHoFr
- Bảng chi phí 8 video mẫu: https://claude.ai/artifact/3LXbwgUH3EF4AmHTXWBUie
- Báo cáo tổng hợp CodeMode: https://claude.ai/artifact/DSxBPkqnbk5kZTCTbUcPJx
- Trace Langfuse của video qua CodeMode (chỉ mở được trên máy người dùng, Langfuse chạy local cổng 3100): `http://localhost:3100/project/localization/traces/f290990be8824e52b50c53c0dd136483`

## 1. Trạng thái

| Mạch việc | Trạng thái |
|---|---|
| Dubber CodeMode (`DUBBER_MODE=code`) | Xong, đã chạy thật (4 lần gọi model, 3 lần `run_code` trên video 7665). **Mặc định trong code vẫn là `single`**; chế độ code đang bật qua docker-compose |
| Trace Langfuse gọn (prompt, code, tool call) | Xong. Bật nội dung bằng `TRACE_AGENT_CONTENT` |
| Sửa che mặt khi không có phụ đề | Xong, có test `test_a_video_with_nothing_to_caption_gets_no_cover` |
| Cổng quảng cáo `skipped_ad` (patch `skip-advertisements`) | Xong, đã chạy thật trên 2 mẫu |
| Cổng 0: `skipped_no_video` và `published_original` (patch `publish-originals-without-voice`) | Xong, đã chạy thật trên 4 mẫu |
| Bộ test | 3200 passed, 7 deselected; ruff sạch (lần chạy cuối của sub-agent cổng tiếng nói) |
| Blur + dịch chữ chèn trên video | **Người dùng hoãn** ("Tạm thời chưa cần") |
| Phase 10, mutation harness | Chưa làm (quy ước: mutation chạy một lần sau phase 10) |

Kết quả 8 mẫu (mỗi dòng là thế hệ chạy hôm nay):

| Nhóm | Video (aweme) | Kết quả |
|---|---|---|
| Không có luồng video | 7441427591061392700 (g2), 7452646214857788731 (g1) | `skipped_no_video` |
| Không có voice | 7670856992643240314 (g2), 7518248980598639930 (g3) | `published_original` |
| Quảng cáo | 7616569292510948709 (g11), 7618513827751629177 (g11) | `skipped_ad` |
| Video thường | 7664547853368951397 (g3), 7665948504426075877 (g7) | `published` |

Cả 8 file đã được chép vào `data/review/` (bản dịch dạng `<id>-g<gen>.mp4`, các video bị bỏ qua dạng `<id>-<trạng thái>.mp4`, là bản gốc). Chi phí 8 video: LLM ≈ $0,012, GPU 419 giây (≈ $0,047 nếu $0,40/giờ).

## 2. Việc tiếp theo

Việc người dùng chờ:
1. Người dùng xem 8 file trong `data/review/` và đánh giá chất lượng, nhất là 2 video dịch (`7664…-g3`, `7665…-g7`).
2. Người dùng xác nhận việc bỏ qua `7452646214857788731` (file chỉ có âm thanh, 13,0% có tiếng nói) và `7518711366992743739` (20,6%) là đúng. Hai file này là âm thanh có thể là giọng thật, nhưng không có hình để dịch.

Quyết định chưa chốt (cần người dùng):
- **Mặc định Dubber:** `code` hay `single`. Kèm đề xuất sửa chỉ dẫn để Dubber thử nhiều bản rút gọn ngắn trong một `gather`, vì ở mẫu 7575 nó đã tốn 3 lần viết lại gần giống nhau.
- **Commit:** 132 file chưa commit. Đề xuất chia theo tính năng: Dubber CodeMode; trace; sửa che mặt; cổng quảng cáo; cổng 0; spike và script eval; docs.
- Có nên chạy đủ 215 video của kênh bây giờ không, hay xem chất lượng 2 video dịch trước.
- Vbee workflow có cần các cổng tương tự không.
- Nguồn tiếng Anh: chưa có cổng ngôn ngữ nguồn. Ở đợt đầu, 5 trong 9 video xong ở `needs_human` (nguồn tiếng Anh, STT nghe sai nặng, Dubber tràn).

Việc kỹ thuật còn lại:
- Sửa `eval/run_experiment.py` (docstring sai), sửa target mutation trước phase 10 (xem handoff cũ mục 1).
- `ConversationSizeGuard` và các gói ảnh của harness; vòng sửa thứ hai cho Localizer; `include_model_request_parameters=False`.
- `schedule_to_start_timeout` cho queue `voice_check`.
- Hiển thị danh sách cho các run `skipped_ad` và `published_original`.
- Tìm nguyên nhân cột `runs.llm_cost_usd` lệch tổng `llm_calls`.
- Ghi chi phí GPU từng run vào DB (hiện chỉ suy ra từ lịch sử Temporal; cần người dùng đồng ý vì là thay đổi code).
- 5 video gần ngưỡng 5% nên nghe lại: `7414328184545479987`, `7656296370752721061`, `7635595666248862841`, `7446990734089080124`, `7598396587995682298`.
- 3 video mới tải: 2 còn ở local, chưa chuyển vào MinIO.
- Xoá `data/review-removed-261030/` (417 MB) khi người dùng cho phép.

## 3. Cách chạy

- **Không tự chạy video.** Người dùng bật máy GPU rồi mới chạy.
- **Máy VoiceStudio trên Vast.ai: không Stop, không bảo người dùng Stop.** Máy Stop không giữ GPU; Start lại có thể kẹt "Scheduling" hàng giờ tới hàng tuần (đã xảy ra ngày 2026-10-30). Muốn dừng, chép dữ liệu giọng ra trước (xem memory `vast-ai-stop-can-lose-the-gpu`). Dòng cũ trong handoff 2026-10-30 đã được sửa.
- **Mỗi lần URL tunnel đổi:** cập nhật `VOICESTUDIO_URL` trong `.env`, rồi tạo lại **`worker-agents` và `localization-starter`** (starter cũng gọi VoiceStudio). Kiểm: `/health` 200, `/v1/models` có `whisper-1`, `VOICE_ID` có trong `/v1/audio/voices`. Hiện `VOICE_ID=455a3773`.
- **Langfuse** là các container `langfuse-web` (cổng 127.0.0.1:3100) và `langfuse-worker`. Phải bật *trước* khi chạy video, nếu không các bước đầu không có trace (đã xảy ra lần này).
- **Hàng đợi (outbox):** `localization_outbox`, trạng thái pending/dispatched/rejected/dead, `MAX_OPEN_WORKFLOWS=10`, claim theo `ORDER BY next_attempt_at`. Đang còn **192 dòng pending bị giữ** bằng `next_attempt_at = now() + interval '30 days'`. Muốn chạy chúng, đặt lại `next_attempt_at`. Sao lưu bảng trước (`pg_dump`; bản mới nhất ở `data/backups/localization_outbox-261031-1151.sql`).
- **Đẩy lại một video:** tăng `generation` lên 1, đặt `state='pending'`, `next_attempt_at=now()`, `dispatched_at=null`, `workflow_id=null`, `dispatch_attempts=0`, `last_error=null`.
- **Chép kết quả ra `data/review/` ngay khi từng video xong** (memory `copy-each-result-to-review-as-it-finishes`), kể cả video bị bỏ qua. Script theo dõi nằm trong scratchpad của phiên và sẽ mất; khi tự viết lại, truyền thời điểm bắt đầu **có múi giờ** (`...Z`), nếu không Temporal trả 400 và script chỉ ghi lỗi vào log. Worker không đọc được `output/` (AccessDenied): lấy video qua `render_key` trong activity `render`, hoặc xuất file qua `docker exec ... cat`.
- **Postgres:** user `reup`, db `reup` (`docker compose exec postgres psql -U reup -d reup`). Khi dùng `docker exec` mà truyền stdin phải thêm `-i`.
- **Chi phí:** LLM đọc từ `localization.llm_calls` (cộng theo `aweme_id` và `generation`). GPU chưa được ghi vào DB; cách suy ra: cộng thời gian chạy của các activity ở queue `stt` và `tts` trong lịch sử Temporal.

## 4. Cấu hình hiện tại (chỉ ghi tên biến)

- `LOCALIZATION_PIPELINE=voicestudio`
- `DUBBER_MODE=code`
- `AD_GATE_ENABLED=true`, `AD_GATE_THRESHOLD` (ngưỡng 0,8)
- `VOICE_CHECK_ENABLED=true`, `VOICE_CHECK_MIN_SPEECH_SHARE=0.05`, `VOICE_CHECK_CONCURRENCY=2`
- `TRACE_AGENT_CONTENT`
- Concurrency queue: media 2, render 1, stt 1, tts 2, llm 8, io 2, voice_check 2. Đã quan sát hiện tượng chặn đầu hàng trên queue `media`.
- Các biến khác (`STT_MODEL`, `TTS_MODEL`, `VOICE_ID`, các model LLM, `VBEE_*`): xem handoff 2026-10-30 mục 4.

## 5. Rủi ro còn mở

- **Cổng quảng cáo** có thể loại nhầm video đáng dịch. Ngưỡng 0,8 chỉ được thử trên một vài video. Không lưu lý do nên khó kiểm lại sau.
- **Cổng 0 cho `published_original`:** video có tiếng nói dưới 5% nhưng quan trọng (giọng nhỏ, nhạc to) sẽ bị đăng bản gốc. Silero VAD chạy trên âm thanh gốc, chưa tách nhạc.
- **Vùng che cho video vừa có lời thoại vừa có chữ chèn:** vẫn là một ô tĩnh trên toàn thời lượng, đã hoãn.
- **Dubber CodeMode** có thể viết lại nhiều bản gần giống nhau, tốn token vô ích (xem quyết định ở mục 2).
- **Số đo thời gian cổng 0 "≈ 10 s"** chỉ lấy sơ lược từ 4 mẫu, chưa đo chính xác.
- **Bảng chi phí:** giá thuê GPU ($0,40/giờ) và tỷ giá (25.000 đ/USD) là giả định, chưa đối chiếu hoá đơn. Thời gian GPU rảnh không được tính.
- **Chất lượng bản dịch** của 2 video mẫu chưa ai đánh giá.

## 6. Quy tắc của người dùng (giữ nguyên)

- Trả lời bằng tiếng Việt, câu đầy đủ, kết quả đặt lên đầu.
- Không in secret ra.
- Không commit khi chưa được yêu cầu.
- Sao lưu DB trước khi sửa dữ liệu.
- Dùng `rg`.
- Chỉ ghi markdown vào `plans/` hoặc `docs/`.
- Workflow thay đổi phải có `workflow.patched` và history replay mới.
- Mutation chỉ chạy một lần, sau phase 10.
- Chép kết quả vào `data/review/` ngay khi từng video xong; chép cả video bị bỏ qua.
- "Cái gì chưa làm mà làm được thì làm đi": đừng chờ nhắc.

## 7. Câu hỏi chưa có lời đáp

- Card RTX 3070 (8 GB VRAM) có chạy VoiceStudio local được không? Cần số VRAM đỉnh khi chạy `faster-whisper` cùng `omnivoice` (chạy `nvidia-smi` trên máy 4090 khi đang dịch một video) và biết VoiceStudio có chạy được trên máy local định dùng hay không.
- Giá thuê GPU thật trên Vast.ai là bao nhiêu mỗi giờ?
