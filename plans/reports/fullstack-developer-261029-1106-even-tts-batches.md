# Chia đều batch TTS (Phase B)

Trạng thái: xong. Ngày 2026-10-29.

## Thay đổi

- `services/localization/src/localization/workflows/video_localization.py`
  - Thêm `TTS_PARALLEL = 2`. Comment ghi rõ giá trị này bằng `tts_concurrency` mặc định.
  - Thêm hàm thuần `tts_batches(hashes)`. Hàm chia thành `k = max(ceil(n / TTS_BATCH), min(TTS_PARALLEL, n))` batch, giữ thứ tự, các batch chênh nhau tối đa 1. Danh sách rỗng trả về `[]`.
  - `_synthesize` dùng `tts_batches` khi `workflow.patched(EVEN_TTS_BATCHES_PATCH)`. History cũ không có marker này nên vẫn chia theo cách cũ: các đoạn 10 và một phần dư.
- Tên patch: `EVEN_TTS_BATCHES_PATCH = "even-tts-batches"`.

## History mới

Bộ `services/localization/tests/replay/histories/even-tts-batches/` được ghi bằng `uv run python -m tests.replay.record_histories even-tts-batches`. Kịch bản nằm trong `SCENARIOS["even-tts-batches"]` của `record_histories.py`. Cả ba run đều hoàn tất và đều có marker `even-tts-batches`.

| Kịch bản | Số lần `synthesize_segments` | Ý nghĩa |
|---|---|---|
| `three-lines-in-two-batches` | 4 | Mỗi lượt có 2 batch. Cách cũ chỉ ra 1 batch. |
| `twenty-five-lines-in-three-batches` | 6 | Mỗi lượt có 3 batch, cỡ 9, 8, 8. |
| `dubber-runs-again-for-one-line` | 6 | 7 dòng, chạy 3 lượt vì review gửi lại 1 lần, mỗi lượt 2 batch. |

## Test

- File mới `tests/workflows/test_tts_batches.py`.
  - Có test chia batch theo tham số: 1 → [1], 2 → [1,1], 7 → [4,3], 10 → [5,5], 12 → [6,6], 20 → [10,10], 21 → [7,7,7], 25 → [9,8,8], 40 → 4×10. Test cũng kiểm thứ tự được giữ.
  - Có test bất biến cho n = 1..100: không batch nào quá 10, các batch chênh tối đa 1, và số batch ít nhất là `min(2, n)`. Có thêm test rỗng.
  - Test workflow: script 7 dòng chạy với `tts_limit=2` ra 2 batch cỡ 4 và 3 ở mỗi lượt. Khoảng thời gian chạy thật của 2 batch lượt đầu chồng lên nhau (`max_overlap == 2`), nghĩa là hai batch chạy cùng lúc thật chứ không chỉ được gửi đi cùng lúc.
  - Red trước: test fail ngay lúc import vì `TTS_PARALLEL` và `tts_batches` chưa có.
- `tests/workflows/test_dubbing_stage.py`: sửa hai test vốn giả định cách chia cũ. Thứ tự bước nay có 2 `synthesize_segments` mỗi lượt. Test 25 hash đổi tên, nay kỳ vọng 9+8+8 ở cả hai lượt.
- `tests/replay/test_replay.py`:
  - Test mới kiểm bộ `even-tts-batches` có marker và có đúng số batch như bảng trên.
  - Test mới kiểm mọi history cũ không có marker này.
  - Control mới `test_control_the_even_batches_without_their_marker_break_the_older_histories`. Nó dùng `tests/replay/ungated_batches_flow.py`, một workflow chia đều nhưng không có marker và chạy hết phần synthesis giống bản thật. Control phải bị từ chối khi replay mọi history cũ có synthesis. Ngoại lệ là `phase-06/plain-run`: 25 dòng ra 3 batch theo cả hai cách, mà replay không so input của activity, nên history này không phân biệt được hai cách chia. Bản đầu của control raise ngay sau khi gửi batch, và nó "đỏ" cả với history này. Như vậy chính lệnh raise làm replay hỏng, nên bản đầu không chứng minh được gì; tôi đã sửa lại control.
  - Thêm `EVEN_TTS` vào exclusion của hai test kiểm "history cũ không có marker" (`voice-rate-with-ingest` và `localizer-single-request`), vì bộ mới có các marker đó. Control `UngatedRateWorkflow` vẫn từ chối bộ mới, nên không cần exclude ở đó.

### Lệnh và kết quả

- `uv run pytest -q tests/workflows/test_tts_batches.py`: 12 passed.
- `uv run pytest -q tests/workflows/test_dubbing_stage.py`: 19 passed.
- `uv run pytest -q tests/replay`: 195 passed.
- `uv run pytest -q tests/workflows tests/observability/test_trace_completeness.py tests/workers/test_agent_wiring.py tests/architecture`: 232 passed, 1 failed. Test fail là `tests/workflows/test_prep_stage.py::test_queue_limits_come_from_settings`, với kết quả `{'media': 3, 'render': 1} != {'media': 3}`. Lỗi này đến từ thay đổi render đang làm song song trong `workers/run.py` và `test_prep_stage.py`, không liên quan TTS, và tôi không sửa.
- `uv run ruff check` và `uv run ruff format --check` trên `tests/replay`, `tests/workflows` và file workflow: sạch.

## Ghi chú

- Số batch chỉ thay đổi khi script có 2–10 dòng: cách cũ ra 1 batch, cách mới ra 2. Từ 11 dòng trở lên, cả hai cách đều ra `ceil(n/10)` batch, chỉ khác cỡ từng batch. Replay không so input, nên chỉ những history có 2–10 dòng mới chứng minh được patch là cần. Các history cũ dùng 3 dòng nên chứng minh được. Patch vẫn bắt buộc vì video ngắn rơi đúng vào vùng 2–10 dòng.
- Không chạy video thật, không chạy mutation, không commit.
