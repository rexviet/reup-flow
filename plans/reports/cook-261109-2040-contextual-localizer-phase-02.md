# Phase 02: Localizer có ngữ cảnh và câu unresolved

Ngày 2026-11-09, Asia/Ho_Chi_Minh. Đã hoàn tất phạm vi Phase 02 với `--auto`.
User đã chọn triển khai dù Phase 01 còn gate chưa đạt; starter và rollout review
vẫn chưa bật. Không commit, publish, đổi DB hoặc cấu hình triển khai. Các thay đổi
multilingual/extension và file chưa commit có sẵn được giữ nguyên.

## Hành vi đã thực hiện

- Input cũ mặc định `legacy_render`. `review_project` cùng marker mới
  `contextual-localizer-unresolved` chọn chính sách contextual; activity context ghi
  policy vào history. Worker ở tools mode vẫn dùng single Localizer cho policy mới;
  history cũ giữ strict validation và thứ tự activity cũ.
- Câu mang `translated`, `inferred` hoặc `unresolved`. Hai trạng thái sau cần reason
  không trắng, tối đa 500 ký tự. `inferred` cần lời dịch; `unresolved` cần text đúng
  chuỗi rỗng. JSON blank cũ vẫn decode, nhưng validator Localizer/assembler tiếp tục
  từ chối nó; thiếu ID không được chuyển ngầm thành omission.
- Repair có câu lỗi, tối đa hai hàng lân cận mỗi phía và toàn transcript đã giới hạn
  bởi cap hiện có. Source, glossary, reviewer notes và problem notes chứa source
  tiếp tục nằm trong untrusted block. Duplicate/unknown ID ở policy mới bị từ chối;
  repair thiếu câu hoặc trả blank không hợp lệ không xóa record hợp lệ trước đó.
- Metadata sống qua assessment, repair JSON round-trip, script và Dubber. Rà soát
  phát hiện `model_fields_set` không bảo toàn qua serialization có defaults; merge
  hiện giữ provenance khi repair trả shape translated/default, có regression test
  ở cả unit và Temporal workflow/replay.
- Synthesis/fit/Dubber không xử lý unresolved; dub document giữ ID và reason, clip
  null, duration 0. Nhạc nền được giữ; thiếu nhạc tạo silence đủ thời lượng; toàn
  unresolved với nhạc im lặng không lỗi SilentAudio. Không đọc original vocals.
- Layout hiện có đã bỏ text rỗng; test xác nhận unresolved không tạo caption và vẫn
  giữ row. Không đổi EditSpec/hash/cover. SRT/project builder production thuộc Phase 03.
- Reviewer deterministic/Jev bỏ intentional omission khỏi fidelity/timing retry;
  vision reviewer được thấy status/reason trong untrusted data cùng hướng dẫn phù hợp.
  Contact, inferred và unresolved flags độc lập; các trường hợp cần review đi needs_human.

## Kiểm chứng

| Nhóm | Kết quả |
|---|---|
| Baseline Localizer + assembler trước sửa | 37 passed |
| Localizer, script, synthesis, dub, audio, review checks, input/schema và architecture | 318 passed |
| Workflow one-request + toàn bộ replay cũ/Vbee | 422 passed |
| Layout, vision/Jev/pre-render/dub review và reviewer checks trong image có raqm | 164 passed |
| Localizer + assembler sau sửa merge JSON cuối | 49 passed |
| Temporal contextual repair, missing ID và replay sau sửa merge JSON cuối | 2 passed |
| `uv run ruff check src tests`, `git diff --check` | Pass |

Các nhóm có test trùng nhau; không cộng thành tổng duy nhất. Image
`reup-localization-tests:latest` dùng `/opt/venv/bin/python`, mount source/tests và
migration/schema/font read-only theo đúng cấu trúc `/workspace/services/localization`,
`PYTHONPATH` trỏ source mới. Không dùng package cũ trong image để thay cho source checkout.

Hai assertion ban đầu cần cập nhật theo hợp đồng mới: field policy thêm vào deps và
problem note thêm untrusted block thứ tư. Giữ nguyên exact-field/escaping assertions,
thêm default-policy assertion; chạy lại đều đạt. Một lượt pytest chọn nhầm thư mục
`tests/contracts` không tồn tại nên không collect; đã chạy đúng `test_input_validation.py`,
`test_edit_spec_schema_drift.py` và architecture. Hai lần dựng harness container ban đầu
sai interpreter/path depth; đã sửa mount/executable, không bỏ hoặc giảm assertion raqm.
Counsel xác nhận các sửa này đúng nguyên nhân.

Review acceptance/contracts/regression thực hiện inline theo ak-code-review.
Simplifier chỉ sửa hai file được giao, gom `text_vi` vào base contract và làm rõ
validator; không thay invariant. ✓ Step 3.S: Simplify ran - scoped changes.
Tất cả test sessions kết thúc; các container tạm được `--rm`, không để dev server.

## Giới hạn và phần còn lại

Fixture synthetic có source và oracle metadata, không giả lập expected Vietnamese
cho output-only IDs 5/10/16/25–27. Chưa đánh giá semantic quality bằng model/provider
thật vì chưa có source transcript tương ứng.

Phase 01 vẫn chưa được chấp nhận (Studio Host/Origin và browser close/reopen).
Phase 03–05 chưa triển khai trong lượt này: chưa có durable review bundle, terminal
ready_for_review hoặc production companion/revoice. Việc khai báo mode ở Phase 02
không có nghĩa full review delivery đã sẵn sàng; starter chưa chọn mode mới.

Không có câu hỏi quyết định còn mở trong phạm vi Phase 02.
