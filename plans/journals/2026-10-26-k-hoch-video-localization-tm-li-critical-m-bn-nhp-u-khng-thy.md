---
title: "Kế hoạch video localization: tám lỗi Critical mà bản nháp đầu không thấy"
date: 2026-10-26
summary: "Plan 11 phase cho Temporal workflow lồng tiếng; red-team 4 góc nhìn bắt 8 lỗi Critical, áp dụng toàn bộ"
---

# Kế hoạch video localization: tám lỗi Critical mà bản nháp đầu không thấy

## Chuyện gì đã xảy ra

Mình viết plan `plans/261026-0147-video-localization-workflow` (11 phase, khoảng 18 ngày) từ brainstorm `261026-0750`. Bản nháp đầu qua `ak plan validate` và đọc thấy hợp lý. Red-team gồm 4 reviewer (bảo mật, kiểu hỏng hóc, giả định sai, scope và contract) trả 39 finding, gộp lại còn 28. Trong đó có 8 Critical, và không lỗi nào lộ ra khi chỉ đọc plan.

Tám lỗi Critical:

- **Chạy lại `dub` không dựng lại sub.** Spec không đổi hash nên render dùng lại mp4 cũ, sub lệch với lời đọc. Test của chính plan còn khẳng định hành vi sai này là đúng.
- **Không thể chạy lại một video.** `needs_human` là workflow *completed*, nên reuse policy `ALLOW_DUPLICATE_FAILED_ONLY` chặn tạo run mới. Starter lại coi `AlreadyStarted` là đã dispatch.
- **Lifecycle 7 ngày của `work/` xoá artifact mà workflow đang chạy cần**, gồm cả cache TTS. Khi đó `NoSuchKey` bị retry mãi.
- **Ảnh của Reviewer đi qua message history của `TemporalAgent`** và vượt giới hạn payload. Phương án dự phòng là presigned URL tới MinIO local thì OpenRouter không gọi tới được.
- **`PydanticAIPlugin` âm thầm thay `PayloadConverter` tuỳ biến**, nên payload guard chỉ tồn tại trong test.
- **`localization.outbox` ghi cứng schema** làm vỡ cơ chế test cô lập theo `search_path` của downloader. Lệnh down `DROP SCHEMA ... CASCADE` có thể xoá schema thật trên máy dev.
- **`run-mutations.sh` không chạy được harness mới**: `HARNESS_DIR` ghi cứng, và fingerprint chỉ phủ `*.ts`, `*.sql`, `*.yml`.
- **Bảy service mới không có quy định port hay auth**, dùng chung superuser Postgres và chung Redis đang chứa cookie Douyin.

## Quyết định

- Áp dụng toàn bộ 28 finding.
- Workflow id thành `video-{aweme_id}-g{generation}`.
- Bảng outbox là `localization_outbox`, không kèm schema.
- Guard là `PayloadCodec`.
- Reviewer chạy trong một activity.
- Cache TTS ở `cache/tts/`, không có lifecycle.
- Render dùng key `render-{spec_sha}.mp4`.
- Refactor mutation runner ở phase 03.
- Thêm gate G6 để đo kích thước history của agent.

Người dùng chọn:
- Remotion bản miễn phí (≤3 người).
- Glossary tự dùng, có rào chắn: không ghi đè, chặn URL.
- OCR dự phòng chỉ làm nếu G5 fail.

## Bài học

Bản nháp đầu sai đúng ở những chỗ mà memory của repo đã cảnh báo: test kiểm lời gọi chứ không kiểm kết quả, seam bị vô hiệu, và các con số khớp nhau không phải là bằng chứng. Cả hai lỗi "sub lệch" và "guard bị thay" đều có test xanh đi kèm.

## Bước tiếp theo

- Người dùng cung cấp `VOICESTUDIO_URL` để chạy G2.
- Cook phase 00.
- Diagram cần cập nhật: 4b chạy sau Dubber, và Reviewer chạy trong activity.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
