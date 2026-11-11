---
title: "Lồng tiếng 2 voice theo người nói, Demucs và diarization chạy host"
description: "Gắn speaker/giới tính cho từng câu bằng pyannote trên host, đọc câu nam bằng voice nam và câu nữ bằng voice nữ, chuyển Demucs ra host worker, giữ chỉnh tay voice từng cue."
status: pending
priority: P1
effort: 11d
branch: feat/localization-dubber-code-mode
tags: [feature, backend, infra, media]
blockedBy: []
blocks: []
created: 2026-11-10
---

# Lồng tiếng 2 voice theo người nói, Demucs và diarization chạy host

Nguồn: [brainstorm](../brainstorm/261110-2330-two-voice-dubbing-host-media-worker.md). Pipeline `voicestudio`. Đã qua red-team (xem cuối file).

## Outcome

Video có một nam một nữ được lồng tiếng bằng hai voice VoiceStudio: câu của người nói nam dùng voice nam, câu của người nói nữ dùng voice nữ. Người review đổi voice từng cue trong Studio. Demucs và diarization chạy trên host (M1 Max 64GB).

## Constraints, non-goals, acceptance

Như brainstorm, cộng các ràng buộc rút ra từ code và red-team:

- Contract đều `extra="forbid"` (`contracts/base.py:7`): trường mới chỉ giúp code mới đọc dữ liệu cũ, không giúp code cũ đọc dữ liệu mới. Vì vậy có thứ tự deploy bắt buộc (Phase 2) và mọi process (worker Docker, môi trường Python trên host, bridge) phải build từ cùng một commit.
- Không thêm tham số mới vào activity đã có: workflow gọi activity bằng `args=[...]` và SDK chỉ chuyển kiểu khi đủ tham số (`workflows/video_localization.py:671-672`). Dữ liệu mới đi qua key suy ra được, không qua tham số.
- Mọi đổi hành vi trong `video_localization.py` nằm sau `workflow.patched` mới, kèm replay test trong `tests/replay/`.
- Vbee giữ nguyên `INGEST` (queue `media`); chỉ VoiceStudio dùng queue `host`.
- Non-goal giữ nguyên: segment nhiều người nói lấy người chiếm nhiều thời gian nhất (spike chỉ đo tỉ lệ để báo cáo, không đổi quyết định này).

## Quyết định của người dùng (2026-11-10)

Host worker nối Temporal `127.0.0.1:7233` và MinIO `127.0.0.1:9000`, **loopback, không xác thực**. Red-team đã nêu rủi ro: mọi process local poll được mọi queue, đọc input, trả kết quả giả hoặc xoá namespace; và ffmpeg parse media không tin cậy ngoài sandbox Docker. Người dùng chấp nhận rủi ro; phương án thay thế (service HTTP trên host, hoặc mTLS) không làm. Giảm nhẹ nằm ở Phase 3 và 7: file env riêng mode 0600, identity MinIO riêng, `PATH` cố định, `stop` đóng lại hai cổng, tài liệu bảo mật ghi rõ.

## Bằng chứng đã đọc trong code (ảnh hưởng thiết kế)

- Voice là một giá trị cho cả run: `spec.voice` đi vào `voice_profile`, `voice_rate`, `segment_hashes`/`synthesize_segments`, `measure_fit`, `DubberDeps.voice`, `dubber_ports.py:22,40`, `revoice_synthesize` (`activities/revoice_caption.py:72`), `review_project.py:190,310`, `publish.py:136,152`. Hash cache TTS đã có `voice_id` (`activities/synthesize.py:73`).
- Production chạy `localizer_single` và `dubber_single`/`dubber_code` (`config.py:90-91`, `agents/temporal.py:313,589`); các module này tự tính budget từ `syllables_per_sec` (`localizer_single.py:176,207,289,349,371,374`, `dubber_single.py:116,151,185,202`).
- `SttSegment.id` được đánh lại mỗi lần STT chạy (`activities/transcribe.py:286`), nên mọi dữ liệu gắn theo `segment_id` cũ đi khi transcript tạo lại.
- `INGEST` không có `schedule_to_start_timeout` (`workflows/video_localization.py:142-147`) và Vbee import chính hằng đó (`workflows/vbee_dubbing.py:64,202`).
- pyannote.audio 4.0.7 đòi `torch>=2.8`; 3.3.2 chỉ cần `torch>=2.0` (PyPI). Package đang ghim `torch==2.5.1` (`pyproject.toml:24`).
- Temporal không publish port, MinIO 9000 chỉ ở mạng compose (`docker-compose.yml:273,335-337`); thêm `-f` làm rơi tự động nạp `docker-compose.override.yml`, file đang đặt `LOCALIZATION_DELIVERY_MODE=review_project`.

## Phases

| # | Phase | Depends | Effort | Status |
|---|---|---|---:|---|
| 1 | [Spike trong đúng môi trường sản phẩm](./phase-01-spike-host-ml.md) | — | 1d | Pending |
| 2 | [Contract, voice cast và thứ tự deploy](./phase-02-contracts-voice-cast-deploy-order.md) | 1 | 1.5d | Pending |
| 3 | [Host worker, queue, quyền và vòng đời process](./phase-03-host-worker-and-queue.md) | 1, 2 | 2.5d | Pending |
| 4 | [Diarization hai bước và gán speaker](./phase-04-diarize-and-assign-speakers.md) | 2, 3 | 1.5d | Pending |
| 5 | [Voice, rate và agent theo từng câu](./phase-05-voice-and-rate-per-segment.md) | 4 | 2.5d | Pending |
| 6 | [Studio: speaker và voice từng cue](./phase-06-studio-per-cue-voice.md) | 5 | 1d | Pending |
| 7 | [start-all, tài liệu, kiểm thử live](./phase-07-start-all-docs-live-test.md) | 3-6 | 1d | Pending |

Phase 1 là cổng go/no-go. Nếu pyannote không chạy ổn trên arm64 thì dừng, quay lại brainstorm. Nếu Demucs trên MPS lỗi hoặc lệch CPU thì host vẫn chạy Demucs bằng CPU.

## Extension trace

Biến thể có thật thứ hai: nơi chạy (Docker hay host). Seam duy nhất thêm là role `host` và queue `host` của `workers/run.py`; cùng role đó chạy được trong compose làm đường lui cho `ingest` (không cho `diarize`, vì image không có pyannote). Voice theo giới tính giữ cụ thể (`male`/`female` trong `VoiceCast`), chưa làm registry nhiều voice vì chưa có ca thứ ba.

## Rủi ro chính

- Rate theo voice chạm Localizer và Dubber ở cả mode `single` và `code`: sai ở đây làm câu tràn hoặc quá ngắn (Phase 5 có đo đối chiếu).
- Lệch phiên bản giữa môi trường host và image: giảm bằng thứ tự deploy, build cùng commit và hiển thị commit trong `status`.
- Host worker tắt: run đứng ở `ingest`. Giảm bằng `schedule_to_start_timeout` để run fail rõ ràng thay vì treo.
- Diarization sai trên thoại ngắn và đối thoại dồn: spike đo tỉ lệ thật; người review sửa bằng voice từng cue.

## Câu hỏi còn mở

- `voice_id` voice nam và voice nữ trong VoiceStudio.
- Cách đoán giới tính: pitch hay classifier nhỏ (chốt sau Phase 1).
- Không còn câu hỏi về tool mode: đã chốt từ chối khi có `voice_cast` (xem Validation Log).

## Red Team Review

### Session — 2026-11-10
**Findings:** 37 thô, gộp thành 14 nhóm (13 áp dụng, 1 ghi nhận là quyết định người dùng).
**Severity breakdown:** 4 Critical, 9 High, 1 Medium (sau gộp).

| # | Finding | Severity | Disposition | Applied To |
|---|---|---|---|---|
| 1 | Phase 4 cũ sửa nhầm file: production dùng `localizer_single`, `dubber_single`, `dubber_code`, `dubber_ports`; `estimate_duration` không có id dòng; deps đổi kiểu vỡ history | Critical | Accept | Phase 5 |
| 2 | `diarize` cần transcript nhưng chạy song song transcribe; gán theo `segment_id` cũ đi khi transcript tạo lại; lỗi diarize huỷ cả `_prepare` | Critical | Accept | Phase 4 |
| 3 | pyannote 4.x đòi torch ≥ 2.8 mà package ghim 2.5.1; spike dựng môi trường khác | Critical | Accept | Phase 1, 3 |
| 4 | Mở Temporal 7233 không xác thực; media không tin cậy parse ngoài sandbox | Critical | Ghi nhận: người dùng chấp nhận rủi ro | plan.md, Phase 3, 7 |
| 5 | Queue `host` không có poller thì run treo vô hạn (không có `schedule_to_start_timeout`) | High | Accept | Phase 3, 4 |
| 6 | Vbee dùng chung `INGEST`; bước dọn bỏ `ingest` khỏi `media` làm Vbee treo | High | Accept | Phase 3, 7 |
| 7 | `extra="forbid"` và môi trường host tách rời: lệch phiên bản vỡ decode; câu "bridge cũ bỏ qua" sai | High | Accept | Phase 2, 3, 7 |
| 8 | Revoice dùng `base.voice`; `voice_override` từ trình duyệt phải là enum role; `request_hash` đổi khi thêm trường | High | Accept | Phase 6 |
| 9 | Thêm tham số activity phá chuyển kiểu của run đang chạy | High | Accept | Phase 4, 5 |
| 10 | Identity MinIO: thiếu `infra/minio/init.sh`, thiếu quyền multipart; `HOST_S3_*`, địa chỉ Temporal, `DEMUCS_BIN` trùng config sẵn có | High | Accept | Phase 3 |
| 11 | Host mất hàng rào supply chain (`demucs_safe`, sha256 weights); pyannote in-process giữ secret; file env và canary | High | Accept | Phase 3, 7 |
| 12 | Process mồ côi: Demucs chạy session riêng, pyannote không huỷ được, hai model tranh MPS | High | Accept | Phase 3, 7 |
| 13 | Phase 3 cũ cần `voice_cast` mà Phase 4 cũ mới tạo; starter trong Docker không nhận biến voice mới | Medium | Accept | Phase 2 |
| 14 | Test wiring role `host`, replay đặt sai thư mục, port vẫn mở sau `stop`, `-f` làm rơi override, fallback Docker không có pyannote | High | Accept | Phase 3, 7 |

### Whole-Plan Consistency Sweep (red-team)
Đã đổi: thứ tự phase (contract lên Phase 2), tách `diarize` hai bước, loại `HOST_S3_*` và "địa chỉ Temporal", bỏ bước dọn `ingest` khỏi `media`, `voice_override` thành enum role. Các thuật ngữ cũ (`SpeakerDocument` theo `segment_id`, `HOST_S3_*`, `voice_override`, "bridge cũ bỏ qua", `host-fallback` cho `diarize`) không còn trong các phase; lệnh kiểm tra nằm ở Phase 7.

## Validation Log

### Session 1 — 2026-11-10

**Verification Results**
- Tier: Full (7 phase). Red-team đã chạy với bằng chứng file:line nên bước kiểm tra chỉ giải các thẻ `[UNVERIFIED]` còn lại.
- Claims đã kiểm lại bằng grep/đọc code: 17. Verified: 16 | Failed: 0 | Unverified: 1, đã giải (`work_key_for` ở `storage/object_store.py:49`, đã cập nhật Phase 4).
- Ghi chú đường dẫn: `infra/minio/init.sh` nằm ở gốc repo, không nằm trong `services/localization` (đã ghi rõ ở Phase 3).
- Kiểm tra bên ngoài: pyannote.audio 4.0.7 đòi `torch>=2.8`, 3.3.2 chỉ cần `torch>=2.0` (PyPI).

**Quyết định của người dùng**

| Câu hỏi | Lựa chọn | Quyết định | Lan sang |
|---|---|---|---|
| Host worker tắt thì run `ingest` làm gì? | Fail rõ ràng / tự chạy Demucs trong Docker | Fail rõ ràng sau `schedule_to_start_timeout`, không tự đổi nơi chạy | Phase 3 (đã đúng sẵn) |
| Diarization lỗi hoặc vắng host thì run làm gì? | Voice mặc định + cảnh báo / `needs_human` / fail run | Dùng voice mặc định **và** kết thúc ở `needs_human` kèm lý do | Phase 4 |
| Mode `tools` khi có `voice_cast`? | Từ chối ngay / đổi schema tool | Từ chối ngay khi bắt đầu run; voice nam/nữ chỉ hỗ trợ mode `single` và `code` | Phase 5 |

### Whole-Plan Consistency Sweep (validation)
Đã đối chiếu `plan.md` (câu hỏi mở, bảng phase) với Phase 3, 4, 5. Mọi liên kết phase tồn tại. Thuật ngữ cũ chỉ còn trong dạng phủ định hoặc bảng red-team. Không còn mâu thuẫn chưa giải.
