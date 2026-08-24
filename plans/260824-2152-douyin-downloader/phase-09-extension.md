---
phase: 9
title: Chrome extension MV3
status: not_started
effort: ~0.5d
blockedBy: [3]
milestone: M3
---

# Phase 9 — Chrome extension MV3 · **kết thúc M3**

Chỉ cần Phase 3 (API) ⇒ **làm song song được** với 4–8.

Đọc trước: IDEA.md §8 · brainstorm §8.

---

## 9.1 Vì sao cần extension

`document.cookie` **không** lấy được cookie HttpOnly (`sessionid_ss`, `passport_csrf_token`) — đây thường là lý do request fail dù cookie trông có vẻ đầy đủ. `chrome.cookies` API lấy được hết.

**Đó là toàn bộ lý do extension tồn tại.** Không phải để tiện.

## 9.2 Manifest

```json
{
  "manifest_version": 3,
  "permissions": ["cookies", "alarms", "storage"],
  "host_permissions": ["*://*.douyin.com/*"],
  "background": { "service_worker": "sw.js" },
  "options_page": "options.html"
}
```

## 9.3 Service worker

`chrome.cookies.getAll({ domain: '.douyin.com' })` → ghép `name=value; ...` → `POST {backend}/internal/cookie` với `X-Api-Key`.

`chrome.alarms` mỗi **20 phút** + push lúc `onInstalled`.

Bỏ qua nếu `cookies.length === 0` — đừng push cookie rỗng đè lên cookie tốt đang có.

## 9.4 Options page

Hai field: backend URL, API key → `chrome.storage.local`. Thêm nút **"Push now"** + hiển thị **kết quả lần push gần nhất** (thời điểm + thành công/lỗi) để debug.

Kết quả lần push gần nhất là thứ giúp phân biệt "extension chết" với "backend từ chối" — không có nó thì debug bằng cách đoán.

## 9.5 Không log cookie

Service worker **không** `console.log` cookie. DevTools của extension là log, và log là chỗ cookie không được vào (`plan.md` §0 nguyên tắc 3).

---

## Todo

- [ ] `services/extension/` — manifest · `sw.js` · `options.html` + js
- [ ] `chrome.cookies.getAll` + ghép header
- [ ] `chrome.alarms` 20′ + `onInstalled`
- [ ] Bỏ qua khi rỗng
- [ ] Options: backend URL + API key + "Push now" + kết quả lần cuối
- [ ] README: cách load unpacked

## DoD

- [ ] Load unpacked, đăng nhập Douyin, "Push now" → `/internal/cookie/status` báo `present`, `age_seconds` nhỏ
- [ ] Cookie push có chứa `sessionid_ss` (chứng minh lấy được HttpOnly — **đây là lý do tồn tại của phase này**)
- [ ] Job chạy được với cookie do extension push, **không ai paste tay**
- [ ] Đăng xuất Douyin → push bị bỏ qua, cookie cũ **không** bị đè bằng rỗng
- [ ] Sai API key → options page hiện lỗi rõ
- [ ] Grep `sw.js` + options js: **không** `console.log` cookie

## Risks

| Risk | Xử lý |
|---|---|
| `chrome.alarms` không chạy khi browser đóng | Push lúc `onInstalled` + startup. Badge vàng/đỏ trên UI là lưới an toàn |
| Push cookie rỗng đè cookie tốt | Bỏ qua khi `length === 0`. Có DoD |
| API key nằm trong `storage.local` | Chấp nhận: internal tool, extension chạy trên máy người vận hành |
