---
phase: 2
title: "Ports: ParserClient + Storage"
status: not_started
effort: ~0.5d
blockedBy: [1]
milestone: M1
---

# Phase 2 — ParserClient + Storage

Hai adapter ra thế giới ngoài. Cô lập ở đây để lane (phase 5–6) không biết gì về HTTP hay filesystem.

Đọc trước: solution design §5.3 · §7 · `plan.md` §0 nguyên tắc 2 (không patch parser).

---

## 2.1 `ParserClient`

Bóc envelope `{code, router, data}` → trả `data` (raw JSON của Douyin).

| Method | Route | Trả về |
|---|---|---|
| `resolveSecUserId(url)` | `get_sec_user_id` | `sec_user_id`. **Xử lý luôn short link `v.douyin.com`** — không tự viết parser URL (IDEA.md §3.3 không cần code) |
| `fetchUserProfile(secUserId)` | `handler_user_profile` | nickname. Cũng là **probe** ở phase 7 |
| `fetchUserPostPage(secUserId, maxCursor, count)` | `fetch_user_post_videos` | `{awemeList, hasMore, maxCursor}` — **`awemeList[].video.play_addr.url_list` có sẵn**, đây là nền của F1 |
| `fetchOneVideo(awemeId)` | `fetch_one_video` | `play_addr` mới. **Chỉ gọi trên đường re-resolve** |

**F4 — ghi vào `parser.errors.ts` ngay từ phase này:** parser bắt `except Exception` và trả `ErrorResponseModel` với `message` **mặc định tĩnh** `"An error occurred."`; `str(e)` không bao giờ được truyền vào. Nên **HTTP 400 không mang thông tin gì**. Phase 2 chỉ phân loại thô:

- `ParserUnavailableError` — không kết nối được / timeout
- `ParserRejectedError` — HTTP 400, **nguyên nhân KHÔNG XÁC ĐỊNH**

Việc phân biệt "cookie bị ban" vs "profile private" là **phase 7 (probe)**, không phải ở đây. Đừng đoán ở tầng client.

Dùng `undici`. Timeout rõ ràng. **Không** retry ở tầng client — retry là quyết định của lane, nó mới biết ngân sách rate limit.

## 2.2 `Storage`

```ts
interface Storage {
  put(key: string, stream: Readable, expectedBytes?: number): Promise<{ path: string; bytes: number }>;
  exists(key: string): Promise<boolean>;
  freeBytes(): Promise<number>;
  remove(key: string): Promise<void>;
}
```

`LocalDiskStorage`: key `douyin/{sec_user_id}/{aweme_id}.mp4`, gốc `DATA_DIR`.

**Ghi vào `.tmp` mang tên duy nhất theo lượt claim**, rồi `fs.rename`. `rename` là **điểm commit duy nhất**. Tên `.tmp` phải duy nhất (không phải `{aweme_id}.tmp` cố định) — nếu reaper nhả video mà worker cũ vẫn đang ghi thì hai writer sẽ đè lên nhau. Đây là risk đã ghi ở solution design §10.

`freeBytes()` phục vụ preflight phase 10.

---

## Todo

- [ ] `parser.client.ts` 4 method, bóc envelope
- [ ] `parser.errors.ts` — 2 lớp lỗi + **comment giải thích vì sao 400 vô nghĩa (F4)**
- [ ] `storage.interface.ts` + `local-disk.storage.ts`
- [ ] `.tmp` tên duy nhất theo lượt claim → `fs.rename`
- [ ] Unit test: bóc envelope, map lỗi
- [ ] Integration test với container `parser` thật: `resolveSecUserId` trên URL đầy đủ **và** short link

## DoD

- [ ] `resolveSecUserId` đúng cho cả `douyin.com/user/MS4w...` và `v.douyin.com/xxx/`
- [ ] Parser tắt → `ParserUnavailableError`, **không** phải lỗi chung
- [ ] **Mutation:** bỏ `.tmp`+rename, ghi thẳng file đích → test crash-giữa-ghi **đỏ**
- [ ] **Mutation:** đổi `.tmp` thành tên cố định → test 2 writer đồng thời **đỏ**
- [ ] `freeBytes()` trả số hợp lý (so với `df`)

## Risks

| Risk | Xử lý |
|---|---|
| Client tự retry → đốt ngân sách rate limit của enumerate | **Không retry ở client.** Có test khẳng định 1 call = 1 request |
| Suy diễn nguyên nhân từ 400 ở tầng client | Chỉ có `ParserRejectedError` chung. Phân biệt là việc của phase 7 |
| Không verify `content-length` → file cụt tính là done | `put()` nhận `expectedBytes`, lệch thì throw |
