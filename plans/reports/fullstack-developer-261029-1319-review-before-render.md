# Review trước render, render một lần (Đợt 2, mục E)

Kế hoạch: `plans/261029-1106-faster-render-and-tts/plan.md`. Chưa commit.

## Thứ tự mới

Mỗi vòng: `layout_captions`, rồi `review_before_render` (không render), rồi sửa và lặp như cũ
(`segment_room`, chạy lại Localizer hoặc Dubber). Sau vòng cuối, bất kể kết quả review (đạt,
không đạt, review không làm được, lần chạy lại cho ra cùng video, lỗi không sửa được), workflow
render một lần bản layout cuối cùng, rồi `publish` với render đó. Đầu vào của `publish` giữ
nguyên (outcome, render_key, reasons, review_rounds, verdict, dub, safety_flag).

Lần chạy ghi lại trước thay đổi (không có marker) vẫn đi đường cũ: render mỗi vòng, rồi
`review_render` trên render.

## Patch và activity

- Patch: `review-before-render` (hằng `REVIEW_BEFORE_RENDER_PATCH`), đọc qua
  `VideoLocalizationWorkflow._review_first()` ở đầu `_rounds`.
- Activity mới: `review_before_render(spec, script, dub, spec_ref, round) -> ReviewVerdict | None`
  trong `ReviewRenderActivity`, chạy trên hàng `llm`, đăng ký ở `workers/run.py` cạnh
  `review_render`. Activity cũ `review_render` giữ nguyên tên và đối số.
- Cùng code kiểm tra `_judge`. `_judge` giờ nhận một hàm đo độ lớn âm thanh thay vì
  `LocalVideo`: `review_render` đưa vào `LocalVideo.loudness` (đo trên mp4), còn
  `review_before_render` tải `dub.audio_key` về thư mục tạm và đo trên track lồng tiếng
  (track mà render đưa vào video). Thư mục tạm bị xoá khi xong.
- Render hỏng hẳn sau một review đạt: lỗi đi ra như mọi stage khác, `_record_failure` ghi
  `failed` với lý do của render và `review_rounds=1`, không có gì được publish (có test).

## Chế độ `llm`

Reviewer `llm` nhìn frame và OCR trên video, mà trước render thì chưa có video. Khi được hỏi
trước render, `review_before_render` ở chế độ `llm` trả `None` mà không đọc gì. Workflow khi
đó render vòng này và gọi `review_render` với render, đúng thứ tự cũ cho vòng đó. Sau vòng
cuối, nếu render cuối đã là bản layout cuối (so `spec_sha256`) thì không render lại. Như vậy
chế độ `llm` vẫn chạy được, không cần workflow biết cấu hình của worker (workflow phải tất
định), và quyết định nằm trong lịch sử (kết quả `None` được ghi lại). Chế độ `jev` (mặc định
production) không bao giờ render giữa các vòng. Test `test_review_payloads` chạy activity thật
ở chế độ `llm` qua đường này và vẫn xanh.

## Lịch sử replay mới

Bộ `tests/replay/histories/review-before-render/` (6 lịch sử, ghi bằng
`uv run python -m tests.replay.record_histories review-before-render`):
`published-at-the-first-review`, `dubber-runs-again-for-one-line`,
`needs-a-person-after-two-reviews`, `review-could-not-be-made`,
`render-fails-for-good-after-the-review`, `reviewer-needs-the-render`.

Test thêm trong `tests/replay/test_replay.py`:
- bộ mới mang marker, mỗi vòng `review_before_render`, một `render` sau review cuối rồi
  `publish`; lịch sử `llm` render và review mỗi vòng;
- các bộ cũ thực sự không có marker và không có `review_before_render`;
- control `tests/replay/ungated_review_first_flow.py` (workflow thật, chỉ khác là
  `_review_first()` trả `True` không qua `patched()`) phải hỏng khi replay mọi lịch sử cũ đã
  từng review. Đã kiểm tra lý do hỏng: "scheduled event 'render' does not match activity
  command 'review_before_render'"; và trên bộ mới nó hỏng chỉ vì thiếu marker, tức là khác
  đúng ở phần được gate.
- Loại trừ được cập nhật ở các test cũ giả định chỉ bộ của mình có patch mới hơn (voice rate,
  one-request, glossary, even-batches).
- `tests/workers/test_agent_wiring.py`: các lịch sử mới cũng phải được worker production phục
  vụ đúng hàng.

## Test

TDD: test workflow và activity mới viết trước và đỏ (7/7 workflow, activity thiếu
`review_before_render`), rồi mới sửa code.

- `tests/workflows/test_review_before_render.py` (7): render đúng một lần; render sau review
  cuối; review không nhận render và đọc track lồng tiếng; chạy hai vòng render một lần; trượt
  cả hai vòng vẫn render một lần cho người xem; review không làm được vẫn render; render hỏng
  sau review đạt; chế độ cần frame.
- `tests/activities/test_review_before_render.py` (7): đạt khi không có render trong store;
  loudness đo trên track lồng tiếng (track nhỏ cạnh render to, nhắm mỗi bên cho kết quả khác
  nhau); cùng các kiểm tra; track mất là `ArtifactMissingError`; dọn thư mục tạm; `llm` trả
  `None`; tên và đối số activity.
- Sửa test cũ theo thứ tự mới: `test_review_loop.py`, `test_unfixable_review.py`,
  `tests/observability/test_trace_completeness.py`, fake `tests/support/review_fakes.py`,
  `tests/support/prep_worker.py`.

Kết quả: `uv run pytest -q tests/workflows tests/replay tests/activities tests/workers
tests/observability/test_trace_completeness.py`: 739 passed (6 phút 50 giây). Riêng
`tests/replay`: 241 passed; `tests/workers`: 56 passed. Chưa chạy mutation (để đến cuối theo
quy ước) và chưa chạy cả suite (controller chạy sau).

`ruff check` và `ruff format --check` sạch trên 14 file đã chạm.

## Lo ngại

- Review không còn thấy những gì chỉ có trên render: lỗi của chính renderer (frame đen, chữ
  vẽ sai font hoặc lệch chỗ so với spec, cover vẽ sai), và loudness sau khi mã hoá AAC. Các
  kiểm tra `jev` vốn đã không nhìn render (trừ loudness), nên với chế độ mặc định chỉ mất phần
  loudness-sau-mã-hoá; renderer giữ kiểm tra riêng của nó (font, deadline).
- Script mutation `tests/mutations/phase-08.sh` (không thuộc quyền sửa) có hai đích đã cũ:
  dòng 324 tìm `except ActivityError as error:\n                # The video was made and nobody
  could judge it`, dòng 508 tìm dòng `QueueSpec("llm", ... activities=[review.review_render] ...)`.
  Cả hai đã đổi. Nên thêm mutation cho gate mới (ví dụ `review_first = self._review_first()`
  thành `False`, bỏ render cuối) khi chạy mutation ở cuối.
- Worker agents giờ tải track lồng tiếng (wav) cho mỗi vòng review thay vì tải mp4; cần ffmpeg
  như trước.
