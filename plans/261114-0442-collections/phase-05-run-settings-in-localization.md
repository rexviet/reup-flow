---
title: "Phase 5: Setting theo lượt chạy trong localization"
status: completed
priority: P1
effort: "2d"
dependencies: [2]
---

# Phase 5: Setting theo lượt chạy trong localization

## Overview

Starter chụp setting của collection vào `LocalizationInput`. Setting được dùng ở cả luồng render cũ
(EditSpec) lẫn luồng review (review project, Studio, revoice, render bản cuối trên Windows). Glossary
theo collection. Màu chữ mặc định thống nhất vàng `#ffff00`.

## Key Insights

- `LocalizationInput` (`contracts/models.py:46-70`) dùng `extra="forbid"`, `frozen=True`; field mới
  phải tùy chọn có mặc định, như `delivery_mode` và `source_snapshot`.
- Production chạy `review_project` (`docker-compose.override.yml:12`). Bản cuối render trên Windows từ
  HTML của review project (`review_worker/final_render.py:300-308`). HTML vẽ lớp che phụ đề gốc là mờ
  trắng 25% cố định (`activities/review_project.py:114`) và chữ vàng (`:112`). <!-- red-team: review path -->
- Studio dựng lại HTML ở mỗi lần lưu (`review_bridge/repository.py:113-117`, gọi từ
  `review_bridge/service.py:91`), chỉ từ manifest. Revoice trộn lại cả track nhạc bằng `render()` không
  có spec (`activities/revoice_caption.py:138-148`); `RevoiceActivities` dựng một lần mỗi process
  (`workers/run.py:158-160`). Vì vậy setting phải nằm trong `ReviewProjectManifest`, không chỉ trong input.
- Nhạc: `balance_music_gain` (`assemble_audio.py:211`) kẹp ±24 dB; `render()` gọi nó ở `:273`.
- Màu nền luồng cũ đang tiêm lúc khởi động worker (`workers/run.py:150-156`), không theo lượt chạy.
- `HexColor` chỉ nhận chữ thường (`contracts/models.py:375`); `Cover.opacity_pct` là số nguyên để spec
  không chứa số thực. Hash bỏ field `False` để giữ hash cũ (`contracts/spec_hash.py:11-20`).

## Requirements

- `CollectionSnapshot`: `id`, `name`, `bgm_offset_db: float | None`, `caption_color: HexColor | None`,
  `cover_color: HexColor | None`, `cover_opacity_pct: int | None` (0–100).
- `LocalizationInput.collection: CollectionSnapshot | None = None`.
- Starter: `VIDEO_SQL` (hai nơi gọi, `outbox_starter.py:345`, `:400`) join `collections` theo
  `videos.collection_id`; giá trị đã chữ thường từ DB.
- Glossary:
  - Phạm vi là `spec.collection.id`. Input cũ (không có `collection`, ví dụ khi bấm "Go on" một lượt
    chạy lỗi trước migration) dùng `channel_id`; `GlossaryStore` tra `collections` để đổi
    `sec_user_id` thành id collection kênh. Đây là truy vấn trong activity nên replay không bị ảnh hưởng.
  - Đổi SQL sang `collection_id`; FK chặn ghi mồ côi; giới hạn `MAX_WRITES_PER_RUN` giữ nguyên.
  - Agent dùng `ON CONFLICT DO NOTHING` nên thuật ngữ tự đặt không bao giờ bị ghi đè (có test).
    `LOOKUP_ALL_SQL` (giới hạn 100) xếp dòng `workflow_id = 'dashboard'` lên trước.
  - Glossary đọc trực tiếp từ DB, không chụp vào input, nên sửa glossary có hiệu lực ở lần tra tiếp theo.
  - Nơi dùng `channel_id` cho glossary: `agents/localizer.py:252`, `:274`, `agents/localizer_single.py:171`,
    `activities/localizer_ports.py:58-73`, `workflows/video_localization.py:574-577`, `:1082-1085`.
- Nhạc: `render(..., music_offset_db=0.0)`; `gain = clamp(balance + offset, ±MAX_MUSIC_GAIN_DB)`, áp cả
  khi balance trả sớm `0.0`. `assemble_audio` lấy offset từ `spec.collection`; revoice lấy từ manifest.
- Luồng cũ (EditSpec): hộp nền lấy từ bản chụp, thiếu thì env; `Caption` thêm `caption_color` tùy
  chọn; layout mới luôn ghi (bản chụp, thiếu thì `#ffff00`); spec cũ thiếu field render trắng và giữ hash.
- Luồng review: `ReviewProjectManifest` thêm `caption_color`, `cover_color`, `cover_opacity_pct`,
  `bgm_offset_db`, đều tùy chọn và đều dùng `HexColor` hoặc số nguyên.
  - `project_html` dùng `caption_color`, thiếu thì `#ffff00`.
  - Có `cover_color` thì `.source-mask` là hộp màu đặc với độ mờ đã đặt, không blur; thiếu thì giữ mờ
    trắng 25% như hiện tại để project cũ không đổi.
  - `review_bridge/repository.py` `prepare()` đọc các giá trị này từ manifest gốc; `revoice_caption._mix`
    truyền `bgm_offset_db` vào `render()`.
- Xuất lại `contracts/edit-spec.schema.json` và `contracts/review-project.schema.json`.

## Related Code Files

- Modify: `services/localization/src/localization/contracts/models.py`, `contracts/spec_hash.py`,
  `contracts/review_project.py`, `contracts/export.py`
- Modify: `services/localization/src/localization/starter/outbox_starter.py`
- Modify: `activities/glossary.py`, `activities/localizer_ports.py`, `agents/localizer.py`,
  `agents/localizer_single.py`, `workflows/video_localization.py`
- Modify: `activities/assemble_audio.py`, `activities/revoice_caption.py`
- Modify: `activities/layout_captions.py`, `activities/caption_images.py`, `activities/render.py`,
  `activities/review_project.py`, `workers/run.py`
- Modify: `review_bridge/repository.py`, `review_bridge/service.py`
- Modify test và double: `tests/support/localizer_fakes.py:68`, `tests/support/kill_worker.py:32`,
  `tests/agents/test_localizer_workflow.py:269`, nơi tạo `LocalizerDeps` trong
  `tests/activities/test_source_evidence.py:41,94`, `tests/support/ungated_single_flow.py:21`,
  `tests/agents/test_answer_only.py:143`, `tests/agents/test_one_request_agents.py:28`
- Modify: `contracts/edit-spec.schema.json`, `contracts/review-project.schema.json`
- Create: test Python đọc `contracts/glossary-term-cases.json` (parity với phase 4)

## Implementation Steps

1. Test trước:
   - Starter: input mang đúng bản chụp; sửa collection sau dispatch không đổi input đã gửi; chạy
     `VIDEO_SQL` bằng role `localization` thành công.
   - Glossary: A không lẫn B; input cũ có `channel_id` tra ra thuật ngữ của collection kênh; ghi với
     khóa không tồn tại bị FK từ chối; thuật ngữ tự đặt không bị agent ghi đè; giới hạn 20 lần ghi.
   - Nhạc: offset −6 đo được trên file ffmpeg ra; offset vượt 24 bị kẹp; track không có giọng vẫn nhận
     offset; revoice một câu giữ nguyên mức nhạc đo được.
   - Màu luồng cũ: spec cũ cùng hash như trước; ảnh caption đúng màu (đọc pixel); contract từ chối màu
     viết hoa.
   - Màu luồng review: HTML có đúng `caption_color` và hộp nền; sau `prepare()` của Studio vẫn đúng;
     project cũ không có field ra HTML như trước.
   - Replay: toàn bộ `tests/replay` vẫn xanh.
2. Sửa contract, starter, glossary, nhạc, màu luồng cũ, luồng review và Studio theo thứ tự trên.
3. Xuất lại schema JSON; `tests/test_edit_spec_schema_drift.py` xanh.

## Todo

- [x] Contract `CollectionSnapshot`, field tùy chọn ở input, EditSpec, manifest
- [x] Starter chụp setting
- [x] Glossary theo collection, map khóa cũ, FK
- [x] Độ lệch nhạc, kể cả revoice
- [x] Màu chữ và hộp nền ở luồng cũ và luồng review, kể cả Studio
- [x] Schema JSON, replay, test double

## Success Criteria

- `cd services/localization && uv run pytest` xanh (không tính `live`); replay xanh.
- Mỗi test mới đỏ khi bỏ dòng code tương ứng.

## Risk Assessment

- Worker, `review-bridge` và render worker cũ từ chối field mới (`extra="forbid"`): deploy toàn bộ trong
  khung bảo trì (phase 8).
- Ducking không tuyến tính nên offset không chắc ra đúng số dB sau ducking; test đo trên file thật và
  phase 8 nghe thử.
- Đổi tham số activity có thể làm replay lệch; nếu đỏ thì bọc nhánh mới bằng `workflow.patched`.

## Ghi chú triển khai (2026-11-14)

- `CollectionSnapshot` và các kiểu `HexColor`, `OpacityPct`, `MusicOffsetDb`, `CollectionId` nằm ở
  `contracts/collection.py` mới, vì `models.py` import `review_project.py` (tránh import vòng).
  `models.HexColor` vẫn dùng được.
- `VIDEO_SQL` join `collections` (inner join, `collection_id` NOT NULL); test chạy bằng role
  `localization`. Fixture starter giờ áp migration collections và tạo collection kênh cho mỗi video
  (`tests/support/scratch_db.add_channel_collection`).
- Glossary: phạm vi là `COALESCE(collection_id, collection kênh của channel_id)` trong SQL. Ghi cho
  collection không tồn tại (FK) hoặc kênh không có collection (NOT NULL) trả `rejected` cho agent, không
  làm hỏng activity (xử lý mục L5 của review phase 2). Dòng đọc ra được kiểm lại bằng `check_term` (phương
  án a của review phase 4, H1). Dòng `workflow_id = 'dashboard'` lên đầu trong 100 dòng.
- `LocalizerDeps.collection_id` mới; port nhận `GlossaryScope`. Workflow dựng deps ở một helper
  `_localizer_deps` cho cả lần đầu lẫn rerun. Không cần `workflow.patched`: replay xanh.
- Nhạc: `music_gain_db(balance, offset)` kẹp ±24 dB; `render(..., music_offset_db)`.
- Luồng cũ: `Caption.caption_color` (layout mới luôn ghi, mặc định `#ffff00`); hash bỏ field khi `None`
  (hash spec cũ được ghim trong test). Hộp nền: từng phần collection không đặt thì lấy env.
- Luồng review: manifest có `caption_color`, `cover_color` + `cover_opacity_pct` (đặt cùng nhau, đã điền
  phần thiếu từ env lúc dựng project; `ReviewProjectActivity` nhận `cover_color`/`cover_opacity` từ
  settings), `bgm_offset_db` (số thực bước 0,5 như DB). Project không có setting ra HTML giống từng byte
  như trước (sha256 ghim từ code HEAD).
- Quyết định chưa có trong plan: collection chỉ đặt độ mờ (không đặt màu) vẫn ra hộp đặc màu env, ở cả
  hai luồng, để hai luồng giống nhau.
- `review_bridge/service.py`, `activities/render.py`, `contracts/export.py` không cần sửa: Studio đi qua
  `Repository.prepare`, painter tự lấy màu từ caption, export đã xuất cả hai schema. Test parity
  `glossary-term-cases.json` phía Python đã có từ phase 4.
- `.env.example`: ghi chú `COVER_COLOR`/`COVER_OPACITY` giờ là mặc định cho phần collection không đặt.
  `docker-compose.yml` truyền hai biến này cho cả `worker-agents` (review project), không chỉ
  `worker-media`; test `compose-constraints.spec.ts` khóa lại.
- Câu trả lời "exists" của glossary chỉ đưa `term_vi` đã lưu cho model khi `check_term` nhận dòng đó.
  Ghi glossary không được (collection mất, kênh không có collection) có `log.warning`.
- Cho phase 8: mọi manifest mới có bốn khóa mới (kể cả `null`), nên `review-bridge`, render worker
  Windows và worker Mac phải chạy code mới trước khi bật starter.
- Test `test_deps_carry_keys_and_numbers_never_video_content` vẫn đỏ như ở HEAD (ngoài phạm vi); khi
  sửa cần thêm cả `source_video_key` và `collection_id`.
- Review: [code-reviewer-261114-1900-collections-phase-05](../reports/code-reviewer-261114-1900-collections-phase-05.md).
