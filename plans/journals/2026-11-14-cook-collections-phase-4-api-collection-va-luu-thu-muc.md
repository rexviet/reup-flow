---
title: "Cook collections phase 4: API collection và yêu cầu lưu thư mục"
date: 2026-11-14
summary: API collection, glossary theo collection với bản sao check_term khớp Python, chuyển collection và action save_to_folder; parity được khóa bằng một file ca dùng chung và fuzz vi sai.
---

# Cook collections phase 4: API collection và yêu cầu lưu thư mục

## What happened

Module mới `services/downloader/src/collections/`: `GET/POST/PATCH /api/collections` và glossary
`GET/PUT/DELETE /api/collections/:id/glossary[/:term_zh]`. Dashboard có thêm `?collection=` ở danh sách
video, mỗi video trả `collection` và `mix_episode`, chi tiết trả `folder_export` mới nhất,
`PATCH /api/dashboard/videos/collection` để chuyển tay, và action `save_to_folder` ghi `folder_exports`.

## Những chỗ đáng nhớ

- Bản sao `check_term` bằng TypeScript là phần rủi ro nhất. `re` của Python trên `str` hiểu Unicode còn
  regex JS thì không: `\d` gồm mọi chữ số thập phân, `\s` và `strip()` dùng bộ khoảng trắng của Python
  (có `\x85`), `\b` tính chữ Việt là ký tự từ, độ dài đếm code point. Ca bộ test chung (48 ca) đều xanh
  ngay lần đầu, nhưng fuzz vi sai 180k chuỗi bắt thêm một lỗi mà không ca viết tay nào nghĩ tới:
  dưới IGNORECASE, Python cho `[a-z]` và chữ `i` trong danh sách TLD khớp cả `İ` và `ı`, còn NFKC giữ
  nguyên hai chữ đó. Đã thêm ba ca vào `contracts/glossary-term-cases.json` để khóa lại.
- Review bắt một lỗ mà cả fuzz lẫn ca viết tay đều không chạm tới: Node mang Unicode 17, Python 3.12
  mang Unicode 15.0. Một chữ cái mới (U+1E5D0) là ký tự từ với Node nhưng chưa gán với Python, nên
  `\b` khác nhau và dashboard nhận `"\u{1E5D0}shop dot com"` mà worker từ chối. Fuzz của tôi chỉ rút
  từ BMP và khối cũ nên không thể thấy. Sửa bằng bảng 707 khoảng code point Python coi là chưa gán, sinh
  từ chính Python và có test Python so lại; nâng Python là test đỏ. Fuzz lại có code point ngẫu nhiên
  trên mọi plane: 0 khác biệt.
- Chặn bấm hai lần dựa vào index unique một phần `folder_exports_one_open_idx` và
  `ON CONFLICT ... DO NOTHING`, không dựa vào đọc trước rồi ghi. Test đua giữ một transaction chưa
  commit đã chèn dòng mở, đợi tới khi `pg_stat_activity` thấy lần bấm thứ hai đang chờ khóa, rồi mới
  commit.
- Khóa lưu thư mục đi qua cùng hàm `resolveRenderKey` với link media, nên chỉ file trong thư mục review
  của đúng generation, hoặc `output/<aweme>.mp4` khi run của generation đó ghi đúng khóa ấy, mới được
  đưa cho worker Mac.
- Tiền tố `E..-` chỉ khi collection hiện tại là `mix` và có `mix_episode`; video bị chuyển tay sang
  collection khác giữ số tập nhưng ra tên `{aweme_id}`.
- 36 đột biến phía TypeScript và 2 phía Python (bỏ luật chữ số trong `check_term`, xóa một khoảng khỏi bảng code point) đều làm test đỏ. Một đột biến lúc đầu "bị giết" chỉ vì lỗi biên
  dịch; chạy lại ở dạng hợp kiểu thì test hành vi mới giết nó.

## Decision

- `save_to_folder` nhận `generation` tùy chọn: có thì lưu đúng generation đó nếu đã render, kể cả không
  còn là hiện tại; không có thì lấy generation hiện tại, để thanh thao tác hàng loạt dùng được.
- Route chuyển collection nằm ở `DashboardActionsController`, vì `DashboardController` có test khẳng
  định nó không có route ghi.
- PUT glossary bỏ khoảng trắng hai đầu như `GlossaryStore.upsert`, nhưng không chuẩn hóa NFC theo plan.
  Review chỉ ra tiền đề của plan sai (`upsert` có NFC), nên để người dùng quyết; chưa đổi hành vi.
- `save_to_folder` có `generation` thì chỉ nhận một video.

## Next steps

- Phase 6 đọc `folder_exports` (khóa đã kiểm theo thư mục review, `target_subdir` tương đối).
- Phase 7 dùng các shape trả về ở trên; lỗi validation trả dạng `"<field>: <message>"`.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
