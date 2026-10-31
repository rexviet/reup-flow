# Handoff: video localization, phiên 2026-10-29

Branch `feat/localization-phase-00-infra`, chưa push. Toàn bộ thay đổi đã nằm trong 8 commit, từ `dd3408b` tới `d7040eb`, chia theo mạch việc.

Hãy đọc file này trước, rồi đọc:
- journal `plans/journals/261029-1708-vbee-pipeline-va-tang-toc.md` (nhất là phần cập nhật tối);
- `plans/261029-1106-faster-render-and-tts/plan.md`;
- `plans/261029-1544-vbee-dubbing-pipeline/plan.md`.

Sơ đồ kiến trúc hiện tại: https://claude.ai/artifact/AERLctbPRNFbUJ5cfhHoFr

## 1. Trạng thái

| Mạch việc | Trạng thái |
|---|---|
| Render bằng ffmpeg trong `worker-media` (bỏ Node/Remotion, bỏ `worker-render`) | Xong, đã chạy thật |
| TTS chia đều batch, đọc theo đoạn ≤ 80 ký tự, cache theo `CLIP_METHOD` | Xong, đã chạy thật |
| Review trước render, render một lần (patch `review-before-render`) | Xong, đã chạy thật |
| Pipeline Vbee (`VbeeDubbingWorkflow`, provider, `plan_slots`, slot shortener) | Xong, đã chạy thật, **giữ làm dự phòng** vì đắt (≈ 6.000–7.300đ mỗi video) |
| VoiceStudio trên RTX 4090 thuê ở Vast.ai | Đang dùng; ≈ 240–440đ mỗi video |
| 3 bản sửa cuối: số đọc bằng chữ, số bị cắt giữa hai đoạn, check chữ gốc lâu ≥ 2 s | Code xong, 2937 test pass, **image đã build nhưng container chưa tạo lại**, chưa chạy thật |
| Phase 09 (eval, score) | Phần lớn xong (xem phase file); E4 và hiệu chỉnh ngưỡng Jev chưa làm |
| Phase 10 (nghiệm thu, backfill, docs) | Chưa bắt đầu |
| Mutation harness | Chưa chạy (quy ước: chạy một lần sau phase 10). Cần sửa trước: các tham chiếu tới renderer cũ trong `scripts/run-mutations.sh`, `scripts/test-run-mutations.sh` và `services/downloader/test/mutations/phase-infra.sh`; hai target cũ trong `tests/mutations/phase-08.sh` (dòng 324, 508). Cũng chưa có mutation cho các patch mới |

## 2. Việc tiếp theo

Việc người dùng đang chờ:
1. Người dùng bật lại máy 4090 trên Vast.ai và gửi URL tunnel mới.
2. Cập nhật `VOICESTUDIO_URL` trong `.env`, rồi chạy `docker compose up -d --force-recreate worker-agents worker-media localization-starter`.
3. Kiểm server: `/health` trả 200, `/v1/models` có `whisper-1`, và `VOICE_ID` có trong `/v1/audio/voices`.
4. Chạy 7616 (hiện đang ở generation 9) và xem có được publish thẳng không. Sau đó chạy 7618.

Quyết định người dùng chưa trả lời:
- Sơ đồ: chỉ gom "synthesize" và "Dubber Agent" vào khung "Lồng tiếng", hay đổi hẳn TTS thành tool của Dubber Agent? Đã khuyên không đổi kiến trúc.

## 3. Cách chạy một video thật

- **Không tự chạy video.** Người dùng bật máy GPU rồi mới chạy. Máy Vast.ai đã Stop không giữ GPU và có thể kẹt "Scheduling" hàng giờ tới hàng tuần (đã xảy ra ngày 2026-10-30): trước khi nhắc người dùng Stop, nói rõ rủi ro này và chép dữ liệu giọng ra trước.
- **Mỗi lần URL tunnel đổi**, phải tạo lại **cả** `worker-agents` lẫn `localization-starter`, vì starter cũng gọi VoiceStudio để kiểm giọng (có memory ghi việc này). Sau khi đẩy video vào outbox, kiểm đã có `workflow_id` rồi mới báo là đang chạy.
- **Đẩy lại một video:** sao lưu bảng `localization_outbox` trước (dùng `pg_dump`). Sau đó tăng `generation` lên 1 và đặt `state='pending'`, `next_attempt_at=now()`, `dispatched_at=null`, `workflow_id=null`, `dispatch_attempts=0`, `last_error=null`. Workflow id có dạng `video-<aweme_id>-g<generation>`.
- **Chạy từng video một.** Đợi video trước qua được ít nhất bước STT rồi mới gửi video sau.
- **Đo thời gian:** script `run_timing.py` nằm trong scratchpad của phiên cũ, sẽ mất. Cách tự làm: đọc history qua API Temporal UI `http://localhost:8233/api/v1/namespaces/default/workflows/<id>/history`, lấy hiệu thời gian giữa sự kiện scheduled và completed của từng activity.
- **Lấy video ra xem:** dùng `ObjectStore.from_settings(Settings()).download_file(render_key, ...)` bên trong `worker-media`. Vì `/tmp` là tmpfs, phải xuất file qua `docker exec ... cat > data/review/<id>-gN.mp4`.
- **Kiểm lỗi âm thanh:** cho STT nghe lại từng clip TTS (`dub-segments.json` có `tts_key`) bằng `whisper-1` với `language=vi`. Đây là cách đã tìm ra lỗi giọng clone.

## 4. Cấu hình hiện tại (`.env`, chỉ ghi tên biến)

- `LOCALIZATION_PIPELINE=voicestudio`
- `STT_MODEL=whisper-1` (engine faster-whisper đang bật trên server)
- `TTS_MODEL=omnivoice`
- `VOICE_ID`: giọng clone "rybu" trên server 4090, đã sửa transcript của audio mẫu
- `VBEE_APP_ID`, `VBEE_API_TOKEN`, `VBEE_VOICE_ID`: còn giữ cho đường dự phòng Vbee
- Model LLM: `LOCALIZER_MODEL` và `DUBBER_MODEL` là `openai/gpt-6-luna`; `REVIEWER_MODEL` là `deepseek/deepseek-v4.1-flash`

## 5. Số đo tham chiếu

| Video | T4 Colab | Vbee | RTX 4090 |
|---|---|---|---|
| 7616 (67 s) | 232 s | 168 s | 194 s (g9, `needs_human`: chưa có 3 bản sửa cuối) |
| 7618 (84 s) | 288 s | 274 s, published | 199–221 s, published |

## 6. Rủi ro còn mở

- **Check chữ gốc:** vùng che hụt khoảng 40 px không còn bị bắt. Luật mới chỉ được kiểm trên 2 video.
- **Lỗi tên hãng do STT nghe sai** (ví dụ "5个女博士" thay cho 无垢女博士): code không sửa được.
- **Lần chạy đầu sau khi bật GPU chậm** vì phải nạp model (TTS 85 s). Nên chạy thử một câu để làm nóng trước khi chạy hàng loạt.
- **Tài liệu phụ đề bị ghi đè** cùng key `r0` mỗi lần chạy, nên không giữ được dữ liệu của các lần chạy cũ để so.
- **Bước tách giọng (demucs) có lần chậm tới 90 s.** Chưa rõ nguyên nhân.

## 7. Quy tắc của người dùng (giữ nguyên)

- Trả lời bằng tiếng Việt, câu đầy đủ, kết quả đặt lên đầu.
- Không in secret ra.
- Không commit khi chưa được yêu cầu.
- Sao lưu DB trước khi sửa dữ liệu.
- Dùng `rg`.
- Chỉ ghi markdown vào `plans/` hoặc `docs/`.
- Workflow thay đổi phải có `workflow.patched` và history replay mới.
- Mutation chỉ chạy một lần, sau phase 10.
