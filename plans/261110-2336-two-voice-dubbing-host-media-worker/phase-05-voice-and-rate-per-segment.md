---
phase: 5
title: "Voice, rate và agent theo từng câu"
status: pending
priority: P1
effort: 2.5d
dependencies: [4]
---

# Phase 5: Voice, rate và agent theo từng câu

<!-- Updated: Red Team Review 2026-11-10 - agent single/code/ports là đường production; deps giữ float cũ; không thêm tham số activity -->

## Goal

Mỗi câu được đọc bằng voice theo `gender` của người nói (nam, nữ, hoặc mặc định), và độ dài dự đoán cùng việc Dubber đo, đọc lại, ghép đều dùng đúng voice và tốc độ đọc của voice đó.

## Thiết kế

- Voice của một câu tính ở **một chỗ**: `voice_for(spec.voice_cast, gender_of(segment))` (Phase 2). Mọi activity, port và agent gọi lại hàm này; gender đọc từ `SpeakersDocument` hợp lệ (Phase 4).
- Rate: giữ `syllables_per_sec: float` là rate của voice mặc định trong `LocalizerDeps` và `DubberDeps` (không đổi kiểu, nên payload agent đang chạy vẫn decode). Thêm hai trường tuỳ chọn: `rates_by_gender: dict[str, float] | None` và `speakers_key: str | None`. Không có hai trường này thì hành vi như cũ.
- `voice_profile` (engine) vẫn tính **một lần mỗi run**: hàm này không dùng tham số voice (`activities/synthesize.py:290-292`), nên không nhân theo voice.
- Không thêm tham số cho activity cũ (`assemble_script`, `measure_fit`, `plan_synthesis`, `segment_hashes`): voice theo câu suy ra từ key và script; chỗ cần trả thêm voice thì đổi kiểu trả về sau patch marker và ghi chữ ký trước/sau trong PR.

## Files (đã liệt kê theo grep, không "sửa tất cả")

- Agent production: `agents/localizer_single.py` (176, 207, 289, 349, 371, 374), `agents/dubber_single.py` (116, 151, 185, 202), `agents/temporal.py` (`_dubber_setup`, `run_localizer`, code dubber ở 437-506, 313, 589), `agents/dubber.py` (136, 212, 225), `agents/localizer.py` (103, 217, 306).
- Cổng và activity dùng `deps.voice`/`spec.voice`: `activities/dubber_ports.py` (22, 40), `activities/dub_script.py` (106, 121, 202, 204; gồm `measure_fit`, `segment_room`, `commit_dub`, `clip_durations`), `activities/synthesize.py` (259-268, 301, 314, 324: `segment_hashes`, `synthesize_segments`), `activities/review_project.py` (190, 310), `activities/publish.py` (136, 152).
- Workflow: `workflows/video_localization.py` (390, 494, 522, 644, 662, 1013, 1052): đo `voice_rate` cho từng voice trong cast song song (`_start_rate` cho từng voice), sau patch marker mới.
- Tool mode (`agents/localizer.py:289`, `agents/dubber.py:220`): `estimate_duration(texts)` không có id dòng nên không chọn được rate theo người nói. Đã chốt (validation): khi có `voice_cast` mà mode Localizer hoặc Dubber là `tools`, từ chối ngay khi bắt đầu run với thông báo rõ; voice nam/nữ chỉ hỗ trợ `single` và `code`. Không đổi schema tool. Nơi chặn: chỗ mode đã được biết (khởi động worker hoặc đầu run; mode là `Settings` của worker, `config.py:90-91`), xác nhận khi làm; thêm test cho tổ hợp này.
- Tests: `tests/agents/` (cả mode `single` và `code`: câu nữ dùng rate nữ; thiếu `rates_by_gender` thì như cũ), `tests/activities/` (câu nữ không vừa slot đi qua Dubber rồi `commit_dub` mà không có `ClipMissing`; voice không đổi giữa lần synthesize đầu và sau khi Dubber viết lại), `tests/test_contract_hardening.py`, `tests/replay/`, `simulation/workflow.py` nếu tên activity đổi.

## Steps

1. `gender_of`/`voice_for` dùng chung; test đơn vị.
2. Voice theo câu qua synthesize, hash, `measure_fit`, `commit_dub`, `dubber_ports`; xác nhận hash dùng `voice_id` của chính câu.
3. Rate theo voice vào `localizer_single`, `dubber_single`, code dubber; đọc kỹ từng module trước khi sửa vì đây là chỗ rủi ro nhất.
4. Đo `voice_rate` cho từng voice trong workflow sau patch marker.
5. Xử lý tool mode theo quyết định.

## Validation

- `uv run pytest -q tests/agents tests/activities tests/workflows tests/replay tests/test_contract_hardening.py`.
- Đo (báo cáo): với voice nam và nữ thật, độ dài dự đoán so với độ dài đo sau TTS nằm trong dung sai ghi ở báo cáo; số câu tràn không nhiều hơn mức hiện tại.
- Live: video nam nữ cho hai voice đúng câu (nghe và đối chiếu `voice_id` trong segment).

## Risk / rollback

Rate sai làm tràn hoặc ngắn bất thường. Rollback: bỏ `VOICE_ID_MALE`/`VOICE_ID_FEMALE` (Phase 2), mọi run về một voice.
