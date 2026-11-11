---
phase: 6
title: "Studio: speaker và voice từng cue"
status: pending
priority: P2
effort: 1d
dependencies: [5]
---

# Phase 6: Studio: speaker và voice từng cue

<!-- Updated: Red Team Review 2026-11-10 - role enum thay vì VoiceRef từ trình duyệt; voice của cue được giữ qua các lần revoice -->

## Goal

Companion hiện speaker và voice của từng cue; người review đổi voice (mặc định/nam/nữ) rồi revoice chỉ cue đó. Voice đã chọn được giữ cho các lần revoice sau (kể cả chỉ sửa chữ).

## Thiết kế

- Trình duyệt chỉ gửi `voiceRole: "default" | "male" | "female"` (ranh giới của bridge giữ nguyên nguyên tắc không nhận giá trị tuỳ ý). Worker tự resolve role sang `VoiceRef` từ `ReviewProjectManifest.voice_cast` (Phase 2), do worker ghi, không do trình duyệt.
- Voice hiệu lực của cue là `cue.voice_role` nếu có, ngược lại là voice theo speaker của cue, ngược lại `base.voice`. `revoice_synthesize` dùng voice hiệu lực này thay cho `base.voice` (`activities/revoice_caption.py:72`), nên sửa chữ một cue nữ vẫn giữ voice nữ.
- `request_hash` (`review_bridge/service.py:219-230`) băm voice hiệu lực và chỉ thêm key `voice_role` khi khác `None`, để request ghi trước khi deploy vẫn khớp hash.
- Cue gom nhiều segment (`source_segment_ids`): lấy speaker chiếm nhiều thời gian nhất trong cue.
- `RevoiceCaptionWorkflow.run(request_key)` giữ nguyên chữ ký; dữ liệu đi trong request JSON.

## Files

- Modify `review_bridge/models.py` (`RevoiceCue`, dòng 27-30: thêm `voiceRole` tuỳ chọn kiểu `Literal`), `review_bridge/service.py` (124, 219-230), `review_bridge/server.py` (164), `review_bridge/repository.py` (302-305), `review_bridge/static/app.js` (141, 176: chọn voice cho cue), `activities/review_project.py` (mang `speaker`, `voice_role` và `voice_cast` vào manifest và cue), `activities/revoice_caption.py` (45-46, 72).
- Tests: `tests/review_bridge/test_service.py` (104), `tests/review_bridge/` (bridge từ chối trường voice tuỳ ý như `VoiceRef`/`voice_id`), `tests/support/revoice.py` (81), `tests/integration/test_review_roundtrip.py` (92), `tests/activities/test_revoice_caption.py`, `tests/workflows/test_revoice_caption.py` (request cũ tạo trước khi thêm trường vẫn khớp hash).

## Validation

- Đổi voice một cue rồi revoice: chỉ cue đó có clip mới, cue khác giữ nguyên, `atempo` vẫn ≤ 1,15 như hành vi hiện có.
- Sửa chữ cue nữ rồi revoice: vẫn dùng voice nữ.
- Cue cũ (không có `voice_role`) giữ hành vi cũ.

## Risk / rollback

Chạm contract bridge: trường mới tuỳ chọn nhưng contract đóng (`extra="forbid"`), nên bridge và worker phải cùng phiên bản (thứ tự deploy, Phase 2). Rollback: ẩn lựa chọn voice trong UI; trường vẫn tương thích.
