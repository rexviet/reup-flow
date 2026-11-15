---
title: "Cook collections phase 5: setting theo lượt chạy trong localization"
date: 2026-11-14
summary: "Setting của collection được chụp vào input lúc dispatch và vào manifest review, nên màu chữ, hộp nền và độ lệch nhạc đúng ở cả hai luồng, kể cả sau khi lưu và revoice trong Studio; glossary tách theo collection."
---

# Cook collections phase 5: setting theo lượt chạy trong localization

## What happened

Starter đọc collection của video trong cùng câu `VIDEO_SQL` và ghi `CollectionSnapshot` vào
`LocalizationInput`. Từ đó setting đi theo hai đường:

- Luồng render cũ: `layout_captions` ghi `caption_color` vào từng `Caption` (mặc định vàng) và lấy hộp nền
  từ bản chụp, phần nào trống thì lấy env của worker. Painter vẽ chữ đúng màu; spec cũ không có màu vẫn
  vẽ trắng.
- Luồng review: `build_review_project` chép setting vào manifest (hộp nền đã điền sẵn phần thiếu từ env),
  `project_html` vẽ chúng, và `Repository.prepare` của Studio đọc lại từ manifest gốc. Revoice trộn nhạc
  với `bgm_offset_db` của manifest.

Glossary tra theo `spec.collection.id`; input cũ chỉ có `channel_id` thì `COALESCE` sang collection kênh
ngay trong SQL của activity.

## Những chỗ đáng nhớ

- Studio không đọc input của workflow: mỗi lần lưu, nó dựng lại HTML chỉ từ manifest. Nếu setting chỉ nằm
  trong input thì lần lưu đầu tiên đã xóa màu chữ và hộp nền. Test Studio lưu hai lần rồi kiểm cả manifest
  lẫn `index.html` trong thư mục chiếu.
- Giữ "project cũ ra HTML như trước" bằng cách ghim sha256 của HTML do chính code HEAD sinh cho fixture
  chuẩn, tính trong một worktree sạch. So từng byte mạnh hơn kiểm vài chuỗi CSS.
- Hash spec cũ: ghim một spec viết theo dạng cũ cùng hash code cũ cho ra. `canonical_spec_json` bỏ
  `caption_color` khi nó là `None`, giống cách đã làm với `only_with_captions`.
- `round` bị che bởi tham số `round: int` trong `LayoutCaptionsActivity._build`; tính phần trăm mặc định
  một lần trong constructor.
- Đo độ lệch nhạc bằng tỉ lệ nhạc so với giọng chứ không đo mức tuyệt đối, vì loudnorm kéo cả bản trộn
  về cùng mức. Với bản không có giọng, so nhạc với giọng hát gốc được giữ lại.
- Baseline đầu tiên chạy cùng lúc tôi sửa code nên không dùng được; chạy lại trong worktree HEAD sạch.
- Review bắt một lỗi mà test Python không thể thấy: compose truyền env cho từng worker theo danh sách
  riêng, và `COVER_COLOR`/`COVER_OPACITY` chỉ có ở `worker-media`. Review project dựng ở `worker-agents`
  nên luôn rơi về mặc định trong code. Test env tổng của compose vốn đỏ vì drift có sẵn và dừng ở service
  đầu tiên lệch, nên phải thêm test riêng cho hai biến này.
- Review cũng bắt một lỗ của bộ lọc đọc: câu trả lời "exists" đưa `term_vi` đã lưu cho model mà không
  kiểm. Giờ dòng bị `check_term` từ chối không được hiện.
- 26 đột biến (bỏ từng nhánh) đều làm test đỏ, harness có guard: đoạn mã phải đổi thật, test phải chạy,
  file được khôi phục đúng từng byte.

## Decision

- Collection chỉ đặt độ mờ mà không đặt màu hộp nền vẫn ra hộp đặc, màu lấy từ env, ở cả hai luồng.
  Plan chỉ nói trường hợp có `cover_color`; cách này giữ hai luồng giống nhau.
- `bgm_offset_db` trong manifest là số thực bước 0,5 như DB, không ép thành số nguyên.
- Ghi glossary cho collection không còn tồn tại (hoặc kênh không có collection) trả "rejected" cho agent
  thay vì làm hỏng activity, vì thử lại không sửa được.
- Docstring tool vẫn ghi "channel glossary" để không đổi prompt.

## Next steps

- Phase 8 deploy mọi thành phần cùng lúc (`extra="forbid"`), nghe thử độ lệch nhạc sau ducking.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
