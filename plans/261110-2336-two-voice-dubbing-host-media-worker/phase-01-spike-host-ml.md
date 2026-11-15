---
phase: 1
title: "Spike trong đúng môi trường sản phẩm"
status: pending
priority: P1
effort: 1d
dependencies: []
---

# Phase 1: Spike trong đúng môi trường sản phẩm

<!-- Updated: Red Team Review 2026-11-10 - spike phải dùng đúng lockfile, demucs_safe và transcript chunked thật -->

## Goal

Có số đo thật trong đúng môi trường sẽ ship để quyết định: pyannote chọn bản nào, Demucs trên MPS có ổn không, đoán giới tính bằng cách nào, và segment STT thật có chứa hai người nói thường xuyên đến đâu. Không sửa mã sản phẩm.

## Files

- Create `services/localization/spikes/host-ml/run-spike.py` và `README.md` (thư mục `spikes/` đã tồn tại).
- Báo cáo: `plans/reports/spike-261110-host-ml-demucs-pyannote.md`.

## Steps

1. **Môi trường:** dựng môi trường Python bằng chính `uv sync` của `services/localization` (có `torch==2.5.1`, `pyproject.toml:24`). Thử thêm dependency group `host` có pyannote và ghi kết quả resolve của `uv lock` cho hai nhánh: pyannote.audio 3.3.x (cần `torch>=2.0`) và 4.x (cần `torch>=2.8`). Nhánh 4.x chỉ khả thi nếu nâng torch cho cả Docker và đã chạy lại `tests/test_demucs_safe.py` và `tests/test_dockerfile.py`; nếu không thì dùng 3.3.x. Token HF đọc từ biến môi trường, không ghi file hay log; sau khi tải model, chạy lại với `HF_HUB_OFFLINE=1`.
2. **Demucs:** chạy qua wrapper `localization.providers.demucs_safe` (không dùng CLI `demucs` gốc), cùng một file, `-d cpu` và `-d mps`. Ghi thời gian, bộ nhớ đỉnh, và độ lệch vocals giữa hai bản. Kiểm weights đúng sha256 như `Dockerfile`.
3. **Transcript thật:** sinh transcript bằng chính `TranscribeActivity` ở chế độ chunked (`workers/run.py:170`) trên vocals đã tách, không dùng STT khác.
4. **Diarization:** chạy trên vocals của 3 đến 5 video thật có một nam một nữ, và một video một người. Ghi số speaker tìm được, thời gian xử lý, lỗi trên thoại ngắn.
5. **Giới tính:** so sánh pitch trung bình theo speaker (F0) với classifier nhỏ, đối chiếu nhãn gán tay.
6. **Ghép:** gán speaker cho segment bằng chồng lấp lớn nhất; báo cáo tỉ lệ gán sai và tỉ lệ segment mà mỗi bên nói trên 30% thời lượng (số liệu cho non-goal; không đổi quyết định).
7. Chạy diarization trong subprocess riêng và đo cancel (kill nhóm process), vì Phase 3 cần điều này.

## Validation (cổng go/no-go)

- `uv lock` resolve được cho nhánh pyannote đã chọn với `pyproject.toml` hiện tại, hoặc ghi rõ thay đổi torch cần làm.
- Demucs MPS tương đương CPU thì dùng MPS; không thì host dùng CPU và ghi lý do.
- pyannote tìm đúng số speaker trên đa số video thử; số cụ thể do báo cáo ghi, người dùng duyệt.
- Báo cáo nêu cách đoán giới tính được chọn.

## Risk / rollback

Chỉ tạo file trong `spikes/` và báo cáo. Kết quả không đạt được ghi nguyên văn, không chỉnh ngưỡng để "pass".
