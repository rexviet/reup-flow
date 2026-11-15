---
title: "Phase 1: Đọc 合集 từ dữ liệu Douyin"
status: done
priority: P1
effort: "0.5d"
dependencies: []
---

# Phase 1: Đọc 合集 từ dữ liệu Douyin

## Overview

`aweme.mapper.ts` đọc `mix_info` (合集, số tập) và `author.nickname` (tên kênh) của từng item. Chưa
ghi DB ở phase này; chỉ mapper, hàm làm sạch tên, và fixture thật.

## Key Insights

- Đã xác nhận ngày 2026-11-14: cả `aweme/detail` lẫn `aweme/post` mang `mix_info` trên từng item
  (`mix_id`, `mix_name`, `statis.current_episode`). Kênh `IM-薛珍珠` có 10 video thuộc
  `mix_id 7695338683825653823` và 1 video không có `mix_info`.
- `parser.client.ts:15-16,185` chuyển nguyên item (`Aweme = Record<string, unknown>`), nên không cần
  request thêm (F1, F2 giữ nguyên) và không có danh sách field để khai báo; mapper tự thu hẹp kiểu.
- Không có code nào ghi `profiles.nickname` (`db/jobs.repository.ts:487-488` chỉ ghi `secUserId`).
  `author.nickname` có sẵn trên từng item nên lấy ở đây, không tốn request. <!-- red-team: tên kênh -->
- Mapper theo nguyên tắc "không bịa": `mix_info` thiếu hoặc sai kiểu thì trả `null`, không đoán.

## Requirements

- `toVideoUpsert` trả thêm `mix: { mixId, mixName, episode } | null` và `authorNickname: string | null`.
- `mixId` là chuỗi số khác rỗng; `episode` là số nguyên dương hoặc `null`.
- Một hàm `cleanDisplayName(raw, fallback)` dùng chung cho `mixName` và nickname: NFC, bỏ ký tự Cc và
  Cf (gồm ký tự điều khiển hướng chữ và zero-width), gộp khoảng trắng, cắt còn 200 code point, rỗng
  thì dùng `fallback` (`mixId` hoặc `sec_user_id`). Tên này đi vào UI và DB, không bao giờ vào tên file.
- Fixture thật, đã cắt bớt trường lớn (URL, ảnh), giữ một item không có `mix_info`.

## Related Code Files

- Modify: `services/downloader/src/parser/aweme.mapper.ts`
- Create: `services/downloader/src/parser/display-name.ts`
- Create: `services/downloader/test/fixtures/aweme/user-post-mix-page.json`
- Create: `services/downloader/test/fixtures/aweme/video-detail-mix.json`
- Modify: test của mapper trong `services/downloader/test/parser/`

## Implementation Steps

1. Chụp fixture qua parser của repo khi stack chạy và có cookie (`fetch_user_post_videos` cho kênh
   `IM-薛珍珠`, `fetch_one_video` cho `7689288513854595771`). Cắt các trường không cần, giữ nguyên shape.
2. Test trước: item có `mix_info` ra đúng `mixId`, `mixName`, `episode`; item không có ra `mix: null`;
   `mix_id` rỗng hoặc kiểu số ra `null`; `current_episode` 0 hoặc âm ra `episode: null`; tên dài 500 ký
   tự bị cắt còn 200; tên chỉ có zero-width ra `fallback`; nickname đọc đúng.
3. Chạy test thấy đỏ, sửa mapper, chạy lại thấy xanh.

## Todo

- [x] Fixture thật cho trang enumerate và video lẻ
- [x] Test mapper và `cleanDisplayName`
- [x] Mapper đọc `mix_info` và `author.nickname`

## Success Criteria

- `pnpm --filter @reup-flow/downloader test -- aweme.mapper display-name` xanh; từng test đỏ khi xóa
  nhánh tương ứng.

## Risk Assessment

- Douyin đổi tên trường: mapper trả `null`, video rơi về collection của kênh; không mất video.
