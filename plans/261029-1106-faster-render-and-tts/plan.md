# Render nhanh hơn và TTS chia đều

Trạng thái: đã kiểm trên run thật (2026-10-29, generation 4, mỗi lúc chạy một video).

| | 7616 (67 s) | 7618 (84 s) |
|---|---|---|
| Tổng | 710 → **331 s** | 786 → **271 s** |
| Mỗi lần render | 223 / 203 → 19 / 20 s | 257 / 225 → 28 / 28 s |
| TTS vòng 1 | 117 → 114 s | 69 → 47 s |

- **Nghiệm thu render:** đạt, dưới 30 s mỗi lần. Video ra là h264 1080x1920, 30 fps, đủ độ dài, phụ đề hiển thị đúng.
- **TTS:** 2 batch chạy song song, mỗi cặp câu khoảng 6 s.
- **Vì sao TTS của 7616 không nhanh lên:** request TTS đầu tiên sau STT chờ 79 s. Đây là lần chạy đầu sau khi Colab vừa bật lại. Ở 7618, chạy ngay sau đó, không còn bị chờ. Đây là thời gian khởi động của Colab, không phải lỗi chia batch; nguyên nhân phía Colab chưa được chứng minh.
- **Kết quả review:** cả hai video vẫn `needs_human`, vì còn sót chữ gốc cạnh phụ đề. 7616 còn thêm lỗi câu đọc nhanh và câu cuối bị cắt.

- **Test:** toàn bộ suite localization có 1395 pass. Một test cũ về giới hạn queue đã được sửa vì `worker-media` giờ nhận thêm queue `render`.
- **Docker:** đã build lại các image, gỡ container `worker-render`, và `minio-init` đã chạy lại không còn user `renderer`.
- **Báo cáo của agent:** `plans/reports/fullstack-developer-261029-1106-ffmpeg-render.md` và `plans/reports/fullstack-developer-261029-1106-even-tts-batches.md`.
- **Còn lại:**
  - Harness mutation còn tham chiếu renderer đã xoá: `scripts/run-mutations.sh`, `scripts/test-run-mutations.sh`, `services/downloader/test/mutations/phase-infra.sh`. Sửa trước đợt chạy mutation sau phase 10.
  - Test `compose-constraints` "each added service receives exactly its own environment" đã đỏ từ trước thay đổi này, vì allowlist của `worker-agents` thiếu các biến `*_MODE`, `*_REASONING` và vài biến khác.
  - MinIO đang chạy vẫn còn user `renderer` cũ. Lý do làm: xem `plans/reports/investigation-261029-1052-render-speed.md`. Một video 67 s mất 710 s, trong đó 426 s là render Remotion.

## Quyết định của người dùng (2026-10-29)

1. Render chuyển hẳn sang ffmpeg và bỏ service `worker-render` (Node + Chrome + Remotion).
2. Batch TTS được chia đều, không gửi một request cho cả video.
3. Hai việc do hai sub-agent làm song song. Sau đó người dùng bật Colab và chạy lại 7616 và 7618.

## Phase A: render bằng ffmpeg (agent 1)

- **Phạm vi:** activity `render` (cùng tên, cùng input `{aweme_id, spec_key}`, cùng output `{render_key, spec_sha256}`, cùng task queue `render`) được viết lại bằng Python trong service localization và chạy trong process của `worker-media`. Như vậy workflow không đổi và các history replay không phải ghi lại.
- **Hành vi phải giữ** (từ `services/renderer/src/activities/render.ts` và `remotion/LocalizedVideo.tsx`):
  - Đặt tên key và chống chạy lại: `work/{aweme_id}/render-{spec_sha256}.mp4`; nếu key đã có thì trả luôn.
  - Có deadline, heartbeat, và khi bị huỷ thì giết ffmpeg.
  - Dựng hình: nền đen, video gốc tắt tiếng phủ kín khung, âm thanh lồng tiếng, khung che rgba, phụ đề căn giữa trong `caption_box` với dòng đã ngắt sẵn, `line-height` 1.25, chữ trắng, bóng `0 2px 6px rgba(0,0,0,.75)`, mốc thời gian theo frame.
- **Cách làm:**
  - Mỗi dòng phụ đề được vẽ thành PNG một lần bằng Pillow, dùng cùng file font.
  - Một lượt ffmpeg: `drawbox`/overlay và các lớp overlay có `enable` theo thời gian.
  - Encode libx264 `-preset veryfast -crf 20 -pix_fmt yuv420p`, AAC, `+faststart`.
- **Bỏ:** service `worker-render` trong compose và thư mục `services/renderer`. Sửa các test và cấu hình nhắc tới nó.
- **Không đụng:** `workflows/video_localization.py`, `tests/replay/`, `tests/mutations/`.

## Phase B: chia đều batch TTS (agent 2)

- **Phạm vi:** `_synthesize` trong `workflows/video_localization.py` chia các hash thành `k` batch có kích thước chênh nhau tối đa 1, với `k = max(ceil(n / TTS_BATCH), min(TTS_PARALLEL, n))`, trong đó `TTS_PARALLEL = 2` (bằng `tts_concurrency` mặc định).
- **Ràng buộc:** đặt sau `workflow.patched`; history cũ vẫn replay theo cách chia cũ. Ghi một bộ history mới bằng `tests/replay/record_histories.py`.
- **Không đụng:** phần render, compose, `tests/mutations/`.

## Nghiệm thu

- Test, ruff và replay đều xanh; controller chạy toàn bộ suite sau khi cả hai agent xong.
- Run thật 7616 và 7618 (người dùng bật Colab): render dưới 30 s mỗi lần, TTS dùng 2 batch đều nhau, video đầu ra nhìn như bản Remotion.

## Đợt 2 (2026-10-29, người dùng chốt lúc 13:19)

Bối cảnh: người dùng nghe bản 7616 lần chạy thứ 5 và phát hiện giọng đọc bỏ chữ. STT nghe lại các clip TTS cho thấy chính TTS đã đọc thiếu:
- câu 1 mất "bảy mươi";
- câu 3 mất "còn kết hợp thêm peptide hemoglobin" và chữ "phẩy";
- câu 4 mất "sáu miligam sắt. Mỗi ngày uống".

Ngoài ra, số "4.6" bị cắt đôi giữa hai segment. Toàn bộ suite trước đợt này: 1401 pass.

- **C. Số trong transcript** (agent riêng, `activities/transcribe.py`):
  - Gộp chữ số bị STT tách ra: "3 0岁" thành "30岁", "4 . 6" thành "4.6".
  - Không bao giờ cắt segment ở giữa một con số.
- **D. TTS theo từng câu ngắn** (agent riêng, `activities/synthesize.py`): mỗi dòng được tách thành các câu, mỗi câu một request TTS, rồi ghép lại thành một clip. Workflow không đổi.
- **E. Review trước render** (agent riêng, workflow + review):
  - Review chạy trên audio lồng tiếng và edit spec, trước khi render.
  - Chỉ render một lần, sau khi review xong.
  - Thay đổi nằm sau `workflow.patched`, kèm bộ history replay mới.
- **Để sau:** cho STT nghe lại mỗi clip TTS (đề xuất 1). Người dùng chọn làm C, D, E trước.
