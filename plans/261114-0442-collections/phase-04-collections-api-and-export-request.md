---
title: "Phase 4: API collection và yêu cầu lưu thư mục"
status: completed
priority: P1
effort: "1d"
dependencies: [3]
---

# Phase 4: API collection và yêu cầu lưu thư mục

## Overview

API cho dashboard: xem, tạo, sửa collection; sửa glossary của collection; chuyển collection của video;
lọc video theo collection; action `save_to_folder` ghi dòng `folder_exports`.

## Requirements

- `GET /api/collections`: danh sách kèm số video, kênh sở hữu, setting.
- `POST /api/collections`: tạo collection `manual` (tên, `sec_user_id` tùy chọn, setting).
- `PATCH /api/collections/:id`: sửa tên và setting; `null` nghĩa là quay về giá trị chung.
- `PATCH /api/dashboard/videos/collection`: chuyển một hoặc nhiều video; đặt `collection_manual = true`;
  chỉ ảnh hưởng lượt chạy và render sau; ghi `dashboard_actions`.
- `GET /api/dashboard/videos?collection=<id>`: lọc theo collection; mỗi video trả `collection` (id, tên,
  kind) và `mix_episode`.
- Glossary (không import nhiều dòng, không phân trang ở server; dashboard lọc phía client):
  <!-- red-team: scope -->
  - `GET /api/collections/:id/glossary`: mọi thuật ngữ, kèm nguồn ("tự đặt" khi
    `workflow_id = 'dashboard'`, nếu không là agent và video nguồn).
  - `PUT /api/collections/:id/glossary/:term_zh`: thêm hoặc sửa, ghi `workflow_id` và
    `source_aweme_id` là `'dashboard'`.
  - `DELETE /api/collections/:id/glossary/:term_zh`.
- Action `save_to_folder` trong `POST /api/dashboard/actions`:
  - Nhận `generation`; chấp nhận mọi generation có `final_renders.state = 'rendered'`, không chỉ
    generation hiện tại.
  - Video luồng cũ: dùng `output/<aweme_id>.mp4` chỉ khi `localization.runs.output_key` của đúng
    generation đó khớp (giống `dashboard.media.ts:146-155`); không có SRT là hợp lệ.
  - Collection phải có `save_subdir`, nếu không trả lỗi rõ ràng cho từng video.
  - Chụp vào dòng mới: `target_subdir`, `file_stem` (`E{tập:02d}-{aweme_id}` hoặc `{aweme_id}`),
    `final_key`, `srt_key`. Không tạo dòng mới khi đã có dòng `pending` hoặc `exporting` cùng video và
    generation.
- Chi tiết video trả trạng thái lưu gần nhất (`pending`, `exporting`, `done` kèm danh sách file,
  `failed` kèm lỗi).

## Validation (zod)

- Màu: `^#[0-9a-fA-F]{6}$`, lưu chữ thường (contract `HexColor` chỉ nhận chữ thường).
- `bgm_offset_db`: −24 đến 24, bước 0.5. `cover_opacity_pct`: số nguyên 0–100.
- `save_subdir`: đường dẫn tương đối, mỗi đoạn khác rỗng, `.` và `..`, không có NUL, tối đa 512.
- Glossary: cùng luật với `check_term` trong `activities/glossary.py:70-93`: độ dài
  (`TERM_MAX_CHARS`, `NOTE_MAX_CHARS`), ký tự điều khiển (`_CONTROL_RE`), link, email, số điện thoại
  (`find_contacts`), và luật chữ số (`term_vi` không có chữ số mà `term_zh` không có). Hai thuật ngữ
  được bỏ khoảng trắng rồi chuẩn hóa NFC trước khi kiểm, giống `GlossaryStore.upsert`; ghi chú không
  NFC. <!-- red-team: check_term -->

## Related Code Files

- Create: `services/downloader/src/collections/` (controller, service, schemas, module, glossary)
- Modify: `services/downloader/src/dashboard/dashboard.controller.ts`, `dashboard.repository.ts`,
  `dashboard.schemas.ts`, `dashboard.service.ts`, `dashboard.actions.ts`, `dashboard.media.ts`
- Modify: `services/downloader/src/app.module.ts`
- Create: `contracts/glossary-term-cases.json` (danh sách ca dùng chung cho test hai phía)

## Implementation Steps

1. Test trước:
   - Từng route và từng luật validation; màu viết hoa lưu thành chữ thường; `save_subdir` có `..` hoặc
     bắt đầu bằng `/` bị từ chối.
   - `save_to_folder`: từ chối khi chưa có bản cuối; chấp nhận generation cũ đã render; video luồng cũ
     có `srt_key` NULL; bấm hai lần chỉ có một dòng đang chờ; `file_stem` đúng có và không có tập.
   - Chuyển collection đặt `collection_manual` và ghi `dashboard_actions`.
   - Glossary: thuật ngữ của collection A không xuất hiện khi đọc B; sửa thuật ngữ agent thì
     `workflow_id` thành `'dashboard'`; mọi ca trong `contracts/glossary-term-cases.json` cho cùng kết
     quả ở zod và ở `check_term` (test Python đọc cùng file).
2. Viết module collections và các thay đổi dashboard.

## Todo

- [x] Test API, validation, parity glossary
- [x] Module collections và glossary
- [x] Lọc, chuyển collection, action `save_to_folder`, trạng thái lưu

## Success Criteria

- `pnpm --filter @reup-flow/downloader test` và `lint` xanh.

## Security Considerations

- Mọi route đi qua `api-key.guard.ts`. Key này nằm trong bundle web (`docker-compose.yml:228`), nên nó
  không phải ranh giới bảo mật; ranh giới là thư mục gốc trên Mac (phase 6) và `check_term`.
- Thuật ngữ tự đặt vẫn đi qua `wrap_untrusted` khi vào prompt như thuật ngữ agent.

## Ghi chú triển khai (2026-11-14)

- Shape trả về: `GET /api/collections` → `{items: [{id, kind, external_id, name, channel: {sec_user_id,
  nickname} | null, video_count, bgm_offset_db, caption_text_color, cover_color, cover_opacity_pct,
  save_subdir, created_at, updated_at}]}`; `POST` trả 201 cùng shape; glossary → `{items: [{term_zh,
  term_vi, note, source: 'dashboard' | 'agent', source_aweme_id, created_at}]}`; `DELETE` trả 204.
  Lỗi validation trả 400 dạng `"<field>: <message>"`.
- `PATCH /api/dashboard/videos/collection` nằm ở `dashboard.actions.controller.ts`, không ở
  `dashboard.controller.ts`, vì controller đọc có test khẳng định không có route ghi.
- `save_to_folder`: `generation` tùy chọn. Có thì lưu đúng generation đó nếu đã render (chỉ nhận một
  video); không có thì lấy generation hiện tại, cho thao tác hàng loạt. Khóa lấy qua `resolveRenderKey`
  dùng chung với link media.
- Bản sao `check_term` (`src/collections/glossary-term.ts`) mô phỏng `re` Unicode của Python và bảng
  code point chưa gán của Unicode 15.0 (`python-unassigned.ts`, test Python kiểm bảng này).
- NFC: plan từng ghi Python không chuẩn hóa NFC, nhưng `GlossaryStore.upsert` có NFC trước
  `check_term`. Người dùng chọn đồng bộ: PUT và DELETE giờ NFC hai thuật ngữ (commit `fix` sau phase 6).
- Cho phase 5: cân nhắc chạy lại `check_term` khi đọc glossary. Cho phase 6: shape `files` tương đối,
  không lộ đường dẫn máy trong `error`. Cho phase 7: gửi `generation` từ trang chi tiết.
- Review: [code-reviewer-261114-1745-collections-phase-04](../reports/code-reviewer-261114-1745-collections-phase-04.md).
