---
phase: 9
title: Chrome extension MV3
status: done_with_concerns
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

- [x] `services/extension/` — manifest · `sw.js` · `options.html` + js
- [x] `chrome.cookies.getAll` + ghép header
- [x] `chrome.alarms` 20′ + `onInstalled`
- [x] Bỏ qua khi rỗng
- [x] Options: backend URL + API key + "Push now" + kết quả lần cuối
- [x] README: cách load unpacked

## DoD

- [ ] Load unpacked, đăng nhập Douyin, "Push now" → `/internal/cookie/status` báo `present`,
      `age_seconds` nhỏ — **chưa verify live**. Container `nest` trong compose stack đang chạy
      (14h+ uptime) trả 404 cho `/internal/cookie` (build cũ hơn module cookie thật, dù module
      **có** đăng ký đúng trong `app.module.ts` hiện tại) và không được rebuild/restart theo
      ràng buộc chia sẻ stack với 2 agent khác. Logic đã test đầy đủ (`sw.spec.js`,
      `push-cookie.spec.js`, `api-client.spec.js` — đúng contract thật của
      `cookie.controller.ts`/`cookie.service.ts`/`api-key.guard.ts`). Checklist verify tay ở
      `services/extension/README.md` §"Manual verification checklist".
- [x] Cookie push có chứa `sessionid_ss` (chứng minh lấy được HttpOnly — **đây là lý do tồn tại
      của phase này**) — chứng minh bằng `cookie-header.spec.js` (fixture mô phỏng đúng hình
      dạng thật của `chrome.cookies.getAll`: nhiều domain, có HttpOnly) và
      `push-cookie.spec.js` (outcome-based: chuỗi `sessionid_ss=...` thật sự tới "backend" giả
      lập đúng ngữ nghĩa ghi-đè-vô-điều-kiện của `CookieService.push`). Chưa verify qua Chrome +
      Douyin thật (cùng lý do container ở trên).
- [ ] Job chạy được với cookie do extension push, **không ai paste tay** — chưa verify được:
      cần cả download lane thật (phase 6, `not_started`) chạy job thật lẫn Chrome thật. Ngoài
      phạm vi automation của phase này dù có backend sống.
- [x] Đăng xuất Douyin → push bị bỏ qua, cookie cũ **không** bị đè bằng rỗng — verify bằng test
      outcome-based (`push-cookie.spec.js`, backend giả lập ghi-đè-vô-điều-kiện) + mutation đỏ
      (`test/mutations/phase-09.sh`). Guard **mạnh hơn** chữ trong spec: thay vì chỉ
      `cookies.length === 0`, kiểm tra sự hiện diện cụ thể của `sessionid_ss` — code review bắt
      được rằng cookie đồng ý/locale có thể sống sót qua logout, nên mảng có thể **không rỗng**
      mà vẫn không còn phiên đăng nhập; xem §Outcome.
- [x] Sai API key → options page hiện lỗi rõ — verify bằng assertion **khớp chính xác** message
      (`push-cookie.spec.js`) + mutation đỏ (vô hiệu hoá `assertOk` trong `api-client.js`).
- [x] Grep `sw.js` + options js: **không** `console.log` cookie — test tự động chạy trong CI
      (`no-console-log.spec.js`, cấm **mọi** `console.*` trong toàn bộ code sản xuất, không chỉ
      chỗ đụng cookie) + mutation đỏ chứng minh guard thật sự bắt được một `console.log` mới
      thêm vào.

## Risks

| Risk | Xử lý |
|---|---|
| `chrome.alarms` không chạy khi browser đóng | Push lúc `onInstalled` + startup. Badge vàng/đỏ trên UI là lưới an toàn |
| Push cookie rỗng/vô hiệu đè cookie tốt | Guard kiểm `sessionid_ss` cụ thể, không chỉ `length === 0` — xem §Outcome. Có DoD |
| API key nằm trong `storage.local` | Chấp nhận: internal tool, extension chạy trên máy người vận hành |
| **Mới phát hiện ở review:** `fetch()` từ extension tới `backendUrl` tuỳ ý bị CORS chặn thật (chỉ origin trong `host_permissions` mới được miễn CORS; `*://*.douyin.com/*` không phủ backend) | `optional_host_permissions` + `chrome.permissions.request()` xin quyền tại thời điểm bấm Save/Push now (bắt buộc phải là user gesture — không xin được từ `sw.js`). Xem §Outcome và README §"CORS and host permissions" |

## Outcome

`pnpm test` (trong `services/extension`): **48/48 xanh**, 8 suite, `chrome.*` stub đầy đủ
(cookies/alarms/storage/runtime/permissions). `bash test/mutations/phase-09.sh`: **5/5 mutation
must-fail đều đỏ**, mỗi cái in diff áp được và file được restore từ snapshot pristine (không
dùng `git checkout --` vì package mới hoàn toàn chưa track).

### Review đối kháng bắt 1 lỗ CORS thật + 2 should-fix

Code review (subagent riêng, đối chiếu trực tiếp với `cookie.controller.ts`/`cookie.service.ts`/
`api-key.guard.ts` thật) tìm ra:

1. **Critical — CORS thật.** 37 test ban đầu xanh tuyệt đối vì `fetch` giả lập trong Jest không
   có mô hình CORS, nên toàn bộ suite "pass" trong khi request thật từ Chrome sẽ bị chặn ngay ở
   preflight (backend không cấu hình CORS, và origin của `backendUrl` không nằm trong
   `host_permissions`). Đây đúng loại lỗi "xanh trong CI, vỡ khi chạy thật" mà mutation test
   không bắt được vì nó không phải logic bug — nó là một giả định môi trường sai. Sửa:
   `optional_host_permissions` + xin quyền runtime lúc Save/Push now (file mới
   `lib/backend-permission.js`, 8 test, TDD RED→GREEN đầy đủ).
2. **Should-fix — guard rỗng chỉ đếm `length`.** Cookie đồng ý/locale có thể sống sót qua
   logout nên mảng không rỗng mà vẫn mất phiên. Sửa: guard đổi sang kiểm `sessionid_ss` cụ thể;
   thêm test tái tạo đúng hình dạng đó + 2 mutation (tắt guard hẳn, và làm yếu guard về lại
   `length`-only để chứng minh hai test đang đo hai chuyện khác nhau).
3. **Should-fix — message lỗi backend không giới hạn độ dài** trước khi ghi vào
   `chrome.storage.local`/hiện lên UI. Sửa: cắt ở 300 ký tự trong `api-client.js`, có test.

Tất cả sửa xong, suite xanh lại, mutation harness chạy lại xác nhận cả 5 guard (2 cho
`sessionid_ss`, 1 no-console-log, 2 cho `assertOk`) đều đỏ khi phá.

### Container backend không verify live được

`docker compose ps` cho thấy `nest` đã chạy 14h+ trước khi phase này bắt đầu. `curl` trực tiếp
vào `/internal/cookie` và `/internal/cookie/status` với `API_KEY` thật từ `.env` root đều trả
404 `Cannot POST/GET ...`, dù `CookieModule` **có** đăng ký đúng trong `app.module.ts` nguồn
hiện tại — container đang chạy một build cũ hơn. Không rebuild/restart vì đó là service dùng
chung với 2 agent khác đang chạy song song trong cùng working tree (ràng buộc trong đề bài).
Contract được xác nhận bằng cách đọc trực tiếp source `cookie.controller.ts` /
`cookie.service.ts` / `cookie.errors.ts` / `api-key.guard.ts`, không đoán.
