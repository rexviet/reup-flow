---
title: "Collections: gom video theo 合集 hoặc kênh, setting riêng và lưu vào thư mục"
description: "Mỗi video thuộc một collection (合集 hoặc kênh) với setting riêng về nhạc nền, màu sub, glossary và thư mục lưu trên Mac."
status: in-progress
priority: P1
effort: "8.5d"
branch: feat/localization-dubber-code-mode
tags: [feature, backend, frontend, database, api]
blockedBy: []
blocks: []
created: 2026-11-14
---

# Collections

## Overview

Thêm thực thể `collection` (một 合集 Douyin, một kênh, hoặc một collection tạo tay). Mỗi video thuộc
đúng một collection, gán tự động lúc tải theo thứ tự: chọn tay, 合集 (`mix_info`), kênh. Collection giữ
setting riêng: độ lệch nhạc nền (dB), màu chữ, màu và độ mờ hộp nền, glossary sửa được trên dashboard,
và thư mục con trên Mac. Setting được chụp vào input của workflow lúc dispatch và vào manifest của
review project, nên đúng cả sau khi sửa trong Studio. Nút "Lưu vào thư mục" ghi một yêu cầu; worker trên
Mac tải bản cuối và SRT về thư mục của collection.

Nguồn quyết định: [brainstorm](../reports/brainstorm-261114-1114-collections.md).

## Quyết định đã chốt

- Màu chữ mặc định thống nhất vàng `#ffff00` cho luồng render cũ lẫn luồng review. Spec cũ thiếu field
  vẫn render trắng và giữ hash cũ.
- Tên file khi lưu: `E{tập:02d}-{aweme_id}.mp4` và `.srt`; video ngoài 合集 là `{aweme_id}`.
- File trùng tên thì ghi đè, qua file tạm rồi đổi tên.
- Video đã có trong DB được chuyển vào collection 合集 khi tải lại kênh, trừ video đã chọn hoặc chuyển tay.
- Thư mục lưu là thư mục con dưới `REVIEW_WORKER_EXPORT_ROOT` trên Mac; worker không ghi ra ngoài gốc.
- Migration áp trong khung bảo trì đã dừng mọi service.

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | [Đọc 合集 từ dữ liệu Douyin](./phase-01-start.md) | Completed |
| 2 | [Schema collection và backfill](./phase-02-collections-schema-and-backfill.md) | Completed |
| 3 | [Gán collection lúc tải](./phase-03-assign-collection-at-intake.md) | Completed |
| 4 | [API collection và yêu cầu lưu thư mục](./phase-04-collections-api-and-export-request.md) | Completed |
| 5 | [Setting theo lượt chạy trong localization](./phase-05-run-settings-in-localization.md) | Completed |
| 6 | [Worker lưu thư mục trên Mac](./phase-06-mac-folder-export-worker.md) | Completed |
| 7 | [Giao diện collection trên dashboard](./phase-07-dashboard-collections-ui.md) | Pending |
| 8 | [Rollout và chạy thật](./phase-08-rollout-and-real-run.md) | Pending |

Thứ tự phụ thuộc: 1 → 2 → 3 → 4; 5 cần 2; 6 cần 4; 7 cần 4; 8 cần tất cả.

## Success Criteria

- [ ] Tải lại kênh `IM-薛珍珠`: 合集 `成名的代价` có 10 video tập 1–10, kể cả video đã có từ trước; video
      còn lại ở collection kênh tên `IM-薛珍珠`; chạy lại không tạo collection trùng.
- [ ] Hai collection khác màu chữ, màu nền, độ mờ và độ lệch nhạc cho ra bản cuối khác đúng theo setting,
      ở luồng review trên Windows, kể cả sau khi sửa và revoice trong Studio.
- [ ] Glossary của collection A không xuất hiện khi chạy video của collection B; thuật ngữ tự đặt trên
      dashboard được dùng và không bị agent ghi đè.
- [ ] "Lưu vào thư mục" đặt `E01-<id>.mp4` và `.srt` vào thư mục con dưới thư mục gốc; dashboard hiện
      trạng thái chờ, đang lưu, xong, lỗi.
- [ ] Sau migration, mỗi kênh cũ có collection, glossary cũ dùng được, spec cũ render lại cùng hash, và
      role `localization` đọc được mọi cột nó cần.
- [ ] Một lượt chạy thật end-to-end với collection có setting khác mặc định.

## Dependencies

- Mọi service phải dừng trong lúc migrate và deploy; contract dùng `extra="forbid"`, nên mọi thành phần
  (kể cả `review-bridge` và render worker Windows) phải chạy code mới trước khi starter bật lại.
- Postgres được backup trước khi áp migration (`scripts/apply-migrations.sh` tự làm).

## Red Team Review

### Session — 2026-11-14
**Findings:** 15 sau khi gộp 38 phát hiện của 4 reviewer (13 accepted, 1 rejected, 1 quyết định của người dùng)
**Severity breakdown:** 4 Critical, 5 High, 6 Medium

| # | Finding | Severity | Disposition | Applied To |
|---|---------|----------|-------------|------------|
| 1 | Màu và độ mờ hộp nền không tác dụng ở luồng review (production) | Critical | Accept | Phase 5, 8 |
| 2 | Studio lưu hoặc revoice làm mất màu chữ và độ lệch nhạc | Critical | Accept | Phase 5, 8 |
| 3 | Video đã có trong DB không bao giờ vào 合集, nên `E01` không thể ra | Critical | Accept (người dùng chọn chuyển tự động) | Phase 2, 3, 8 |
| 4 | Khoảng hở giữa migration và deploy làm hỏng tải video và glossary | Critical | Accept (khung bảo trì) | Phase 2, 8 |
| 5 | Role `localization` không đọc được cột mới của `videos`; grant cho `nest` vô nghĩa | High | Accept | Phase 2, 5 |
| 6 | Sót nhiều danh sách migration và consumer viết cứng; câu NFC sai | High | Accept | Phase 2, 4, 5 |
| 7 | Down migration glossary hỏng khi rollback | High | Accept (dùng `workflow_id='dashboard'`) | Phase 2 |
| 8 | Input cũ hoặc "Go on" tra glossary bằng khóa cũ, ghi dòng mồ côi | High | Accept | Phase 2, 5 |
| 9 | Thuật ngữ tự đặt bỏ qua `check_term` | High | Accept | Phase 4, 5 |
| 10 | Không ai ghi `nickname`; tên Douyin chưa được làm sạch | Medium | Accept | Phase 1, 2, 3 |
| 11 | Claim, thread, file tạm, SRT luồng cũ, generation cũ của worker lưu | Medium | Accept | Phase 4, 6 |
| 12 | Worker tin `final_key` từ DB; file tạm có thể đi theo symlink | Medium | Accept | Phase 6 |
| 13 | Độ mờ dạng số thực; `HexColor` chỉ nhận chữ thường | Medium | Accept | Phase 2, 4, 5 |
| 14 | Import nhiều dòng, phân trang server, cột `source` vượt yêu cầu | Medium | Accept | Phase 2, 4, 7 |
| 15 | `save_dir` trỏ tới bất kỳ đâu trong khi API key nằm trong bundle web | Medium | Accept (người dùng chọn thư mục gốc) | Phase 2, 4, 6, 7 |
| — | Bỏ màu vàng mặc định cho luồng cũ | Medium | Reject: người dùng đã chọn thống nhất vàng | — |

Không áp dụng: tách role riêng cho render worker Windows để nó không giả được thuật ngữ "tự đặt". Role
`localization` vốn đã ghi được glossary, nên rủi ro tăng thêm nhỏ và đây là thay đổi hạ tầng ngoài phạm vi.

### Whole-Plan Consistency Sweep

- Đã thay mọi chỗ `save_dir` tuyệt đối bằng `save_subdir` dưới `REVIEW_WORKER_EXPORT_ROOT`; `cover_opacity`
  bằng `cover_opacity_pct`; cột glossary `source` bằng `workflow_id = 'dashboard'`; claim `SKIP LOCKED`
  bằng trạng thái `exporting` có lease.
- Đã bỏ: grant cho role của `nest`, import glossary, phân trang server, câu "chuẩn hóa NFC giống Python",
  "trong cùng transaction với upsertMany", và câu "video cũ không được gán theo 合集".
- Đã đồng bộ tiêu chí nghiệm thu ở `plan.md` với phase 3, 5, 6 và các bước kiểm của phase 8.
- Không còn mâu thuẫn chưa giải quyết.
