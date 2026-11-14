---
title: "Cook collections phase 1: đọc 合集 từ dữ liệu Douyin"
date: 2026-11-14
summary: "Mapper đọc mix_info và author.nickname, thêm cleanDisplayName, fixture thật kênh IM-薛珍珠"
---

# Cook collections phase 1: đọc 合集 từ dữ liệu Douyin

## Đã làm
- `aweme.mapper.ts` trả thêm `mix: { mixId, mixName, episode } | null` và `authorNickname`; kiểu trả về là `MappedAweme extends VideoUpsert`, nên `upsertMany` không đổi (drizzle chỉ ghi cột có trong bảng, đã xác nhận ở `dialect.js`).
- `display-name.ts`: `cleanDisplayName` bỏ Cf, gộp khoảng trắng, bỏ Cc, NFC, cắt 200 code point, rỗng thì dùng fallback.
- Fixture thật chụp qua parser đang chạy: trang `fetch_user_post_videos` của `IM-薛珍珠` (10 tập 合集 `成名的代价` + 1 video lẻ) và `fetch_one_video` của `7689288513854595771`. Cắt về các field mapper đọc; mọi URL chỉ còn scheme và host; đã bỏ `authentication_token`. gitleaks sạch.

## Bẫy gặp phải
- `\s` của JavaScript khớp U+FEFF (là Cf), nên gộp khoảng trắng trước khi bỏ Cf biến BOM thành dấu cách. Test đỏ bắt được; Cf giờ bỏ đầu tiên.
- NFC trước khi bỏ ký tự làm kết quả có thể không còn NFC (`e` + ZWSP + dấu mũ). Chuyển NFC xuống sau.
- `Number()` đọc `'0x10'` là 16 và `'1e1'` là 10; episode có tập riêng `readEpisode`, giới hạn int4 vì một giá trị tràn sẽ làm hỏng cả trang insert ở phase 3.
- Review chỉ ra item đồng sáng tác có thể mang tên tác giả khác: `authorNickname` là null khi `author.sec_uid` khác kênh đang đọc.

## Kiểm chứng
- 56/56 test mapper và display-name; tsc, eslint sạch; suite DB qua `upsertMany` 82/82.
- 22 mutation đều bị giết. Full suite downloader có lỗi sẵn từ trước (compose-constraints, suite gắn cứng cổng 5432, va chạm khi chạy song song trên cùng DB), cũng đỏ trên HEAD khi stash thay đổi.

## Tiếp theo
- Phase 2: schema collection và backfill. Tên cột phase 2 không được trùng key `mix`/`authorNickname`, nếu không drizzle sẽ ghi chúng.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
