---
phase: 2
title: "Ports: ParserClient + Storage"
status: completed
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

- [x] `parser.client.ts` 4 method, bóc envelope
- [x] `parser.errors.ts` — 2 lớp lỗi + **comment giải thích vì sao 400 vô nghĩa (F4)**
- [x] `storage.interface.ts` + `local-disk.storage.ts`
- [x] `.tmp` tên duy nhất theo lượt claim → `fs.rename`
- [x] Unit test: bóc envelope, map lỗi
- [x] Integration test với container `parser` thật: `resolveSecUserId` trên URL đầy đủ — short link **chưa** (xem §Gap)

## DoD

- [x] `resolveSecUserId` đúng cho `douyin.com/user/MS4w...` — **`v.douyin.com/xxx/` chưa verify bằng link sống**, xem §Gap
- [x] Parser tắt → `ParserUnavailableError`, **không** phải lỗi chung
- [x] **Mutation:** bỏ `.tmp`+rename, ghi thẳng file đích → test crash-giữa-ghi **đỏ**
- [x] **Mutation:** đổi `.tmp` thành tên cố định → test 2 writer đồng thời **đỏ**
- [x] `freeBytes()` trả số hợp lý (so với `df`)

## Risks

| Risk | Xử lý |
|---|---|
| Client tự retry → đốt ngân sách rate limit của enumerate | **Không retry ở client.** Có test khẳng định 1 call = 1 request |
| Suy diễn nguyên nhân từ 400 ở tầng client | Chỉ có `ParserRejectedError` chung. Phân biệt là việc của phase 7 |
| Không verify `content-length` → file cụt tính là done | `put()` nhận `expectedBytes`, lệch thì throw |

---
## Outcome

Commit dưới đây. **143 test xanh** (thêm 69 test: 42 cho `ParserClient`, 21 cho `LocalDiskStorage`, 6 cho `PARSER_BASE_URL` ở `env.ts`), **10 integration test** chạy với container `parser` thật, **15 mutation** đều áp được và đều bắt được, **1 control** phải xanh và đã xanh.

Con số 15 (thay vì 8 ở bản đầu) là kết quả của một vòng review đối kháng. Bảy mutation cuối sinh ra từ những chỗ review chỉ ra là "invariant được bảo vệ bằng comment, không bằng test".

### Luật thứ ba cho mutation suite: `run_control`

`test/mutations/phase-02.sh` giữ hai luật của phase 1 (in diff thật sự áp được; match rỗng là **lỗi cứng**) và thêm luật thứ ba: **mutation phải-xanh**. Lý do: mutation đỏ chỉ chứng minh dòng bị đụng *có vai trò gì đó* — nó không nói vai trò nào. Khi hai cơ chế nằm chung một dòng (tên `.tmp` duy nhất **và** cờ `wx`), chỉ có một control gỡ cơ chế yếu hơn mà vẫn xanh mới chỉ ra cơ chế nào đang gánh invariant.

Luật "match rỗng là lỗi cứng" đã tự chứng minh giá trị ngay trong phase này: sau khi đổi `Boolean(...)` thành `toBoolean(...)`, mutation `has_more` cũ match rỗng và suite **báo lỗi** thay vì lặng lẽ đếm nó là pass.

### Ba bài học về false green, đắt hơn code

1. **Test đồng thời hai writer đỏ vì lý do sai — và lý do đó sắp bị phase 6 xoá.** Mutation "tên `.tmp` cố định" đỏ, nhưng không phải vì corruption: writer thua cuộc gọi `rename` trên temp file đã bị writer thắng dời đi, ăn `ENOENT`, và test **chết trước khi kịp chạy tới assertion về nội dung file**. Thêm đúng thứ mà deviation 6 nói phase 6 sẽ thêm — một `rename` khoan dung với `ENOENT` — thì test **flaky-green: 3/6 lần chạy pass** với hai writer thật sự dùng chung một file.

   Nguyên nhân sâu hơn: hai payload **cùng độ dài**, ghi cùng offset, nên writer nào ghi sau cũng có thể để lại một file trông hoàn toàn đồng nhất. Assertion "đếm số byte-value khác nhau" không đủ. Bản sửa cho hai writer **độ dài khác nhau** (8 chunk vs 20 chunk) và đòi file commit **bằng đúng byte-for-byte một trong hai payload** — một file dùng chung không thể bằng payload nào. Thêm mutation thứ ba mô phỏng sẵn hình dạng phase 6 (`rename` + `open` khoan dung ENOENT): đỏ **4/4 lần chạy**.

2. **`bavail` vs `bfree` không thể kiểm bằng filesystem thật.** Test so với `df` có dung sai `max(available×5%, 512MB)`; trên APFS hai con số **bằng nhau đúng 0 byte**, còn trên ext4 phần dự trữ mặc định là **đúng 5%** — ngay biên dung sai. Tức là guard này được bảo vệ bằng một comment mà không test nào bác được, trong khi nó nuôi preflight phase 10. Sửa bằng cách **inject `statfs`**: một test với `{bavail: 100, bfree: 500, bsize: 4096}` ghim lựa chọn một cách xác định trên **mọi** máy. `df` vẫn giữ, làm sanity check thực địa.

3. **F4 có hai hướng, mutation cũ chỉ phủ một.** Mutation "bịa nguyên nhân cho 400" phủ hướng *bịa lỗi*. Hướng còn lại nguy hiểm hơn: fork trả **nguyên văn** mọi body 200 của Douyin rồi bọc `code: 200` của chính nó, nên một từ chối ở tầng ứng dụng Douyin (`{"status_code": 8, "user": null}`) đến đây trông như một trao đổi HTTP hoàn toàn khoẻ mạnh. Probe phase 7 đọc `fetchUserProfile` thành công là "cookie còn tốt" ⇒ **báo cookie bị ban là khoẻ**, rồi đẩy người vận hành đi soi profile. Chính plan đã tự nói 200 ≠ thành công: tín hiệu blocked cấu trúc của phase 5 là `aweme_list` rỗng + `has_more=1` ở **HTTP 200**.

### Một false RED, và nó nguy hiểm không kém false green

Giữa lúc verify lần cuối, `pnpm test` đỏ 2 test rồi 5 lần chạy sau **xanh sạch**. Không bỏ qua. Giả thuyết: `testSchemaName()` của phase 1 đặt tên schema theo **jest worker**, mà worker id **restart từ 1 ở mỗi lần gọi jest** — nên hai lần chạy chồng nhau (hai terminal, watch mode, hai CI job cùng một database) đều nhận `test_migration_w8`, và bên nào chạm `DROP SCHEMA ... CASCADE` trước sẽ **xoá bảng của bên kia giữa test**.

Đã dựng thí nghiệm để xác nhận thay vì đoán: 4 lần chạy chồng nhau ⇒ **22–35 test đỏ mỗi lần, toàn bộ nằm trong 4 suite db**, báo `already exists` và `relation ... does not exist`. Không có gì trong đó đọc ra thành "bạn chạy suite hai lần" — nó đọc ra thành **claim query hỏng**, tức là kết luận sai đắt nhất có thể có trong repo này. Chính comment ở `test/support/db.ts` đã viết rằng cô lập schema tồn tại để tránh đúng thứ này; nó chỉ chưa đi đủ xa.

Sửa hai chỗ, đều là hạ tầng phase trước:

1. `testSchemaName()` thêm nửa **per-run** (`process.pid`). 22–35 đỏ → **0**.
2. `test/spike-skip-locked/db.ts` (phase 0) vẫn dùng `spike_items` trần trong `public` — không worker, không run. Dưới tải chồng, `TRUNCATE` của lần chạy này **chặn sau row lock mà harness của lần chạy kia đang cố ý giữ mở**, hook timeout. Đỏ rơi đúng vào suite mà công việc duy nhất của nó là chứng minh claim đúng dưới khoá đồng thời — nên cách đọc tự nhiên nhất của vệt đỏ là "SKIP LOCKED hỏng". Cho nó đúng lớp cô lập như các suite db.

Sau đó: **4 lần chạy chồng nhau, cả 4 đều 143/143**, và không sót schema nào lại.

### Mười thứ lệch khỏi bản phác §2.1–2.2, có chủ ý

1. **`ParserRejectedError` mang `status`, vẫn chỉ 2 lớp lỗi.** Gộp 404/422 chung rổ với 400 thì phase 7 sẽ probe cookie vì **lỗi gõ sai route của chính ta**. FastAPI validate query signature *trước* thân handler ⇒ 404 = route sai, 422 = tên tham số sai, và **chỉ** request đúng hoàn toàn mới chui vào `try:` rồi ra 400 catch-all. Mang `status` là đủ cho phase 7 mà **không** diễn giải vì sao 400 xảy ra. Hai negative control trong integration suite (route bịa → 404, `secUserId` thay `sec_user_id` → 422) là thứ làm lập luận này trung thực: nếu không có chúng, "400" chỉ là câu trả lời cho mọi thứ.
2. **`statusCode` được surface trên cả ba kiểu trả về** (`UserProfile`, `UserPostPage`, `VideoPlayAddr`) — xem bài học 3. **Surface, không diễn giải**: `null` khi Douyin không gửi, **không** mặc định `0`, vì bịa ra thành công chưa từng quan sát được cũng sai đúng như bịa ra nguyên nhân.
3. **5xx → `Unavailable`, không phải `Rejected`.** Fork không bao giờ trả 5xx: mọi exception của nó thành 400. 5xx đến từ uvicorn hoặc thứ đứng trước ⇒ **chưa từng có phán quyết Douyin nào**. Cùng lý do: 200 mà body không phải envelope cũng là `Unavailable`.
4. **`ParserClient` sở hữu `Agent` riêng + có `close()`.** `undici.request` dùng dispatcher **toàn cục** nếu không được đưa cái riêng; bất kỳ thư viện nào trong process gọi `setGlobalDispatcher(new RetryAgent(...))` là mọi lời gọi parser thành 4 — nhân tần suất của vòng enumerate vốn tin rằng nó gọi 1 lần mỗi 3–5 giây (F2). Đã đo: 1 request → 4. **Không mutation nào của `parser.client.ts` bắt được regression nằm ở file khác**; sở hữu dispatcher là cách kéo invariant về trong tầm với của một mutation. `close()` là thứ graceful shutdown phase 4 cần — một call thành công để lại keep-alive socket trên event loop.
5. **`toBoolean` / `toNumber` thay vì `Boolean()` / fallback 0.** `Boolean("0")` là `true`; Douyin có gửi `has_more` dạng chuỗi. Và `max_cursor` không đọc được mà fallback `0` nghĩa là "quay lại trang một", **mãi mãi** — vòng lặp không tiến, không dừng, đốt đúng ngân sách F2. Giờ: page nào `has_more` bật mà không có cursor đọc được thì **throw**, vì đó là page không dùng được.
6. **`playAddrUrls()` là extractor dùng chung.** Enumerate đọc trên `aweme_list` (F1), re-resolve đọc trên `fetch_one_video`. Hai bản sao sẽ trôi khỏi nhau ngay lần đầu Douyin đổi chỗ trường đó, và bản không ai để ý mới là bản hỏng.
7. **`LocalDiskStorage` từ chối key thoát `DATA_DIR` — hai tầng.** Tầng từ vựng (`resolve`/`relative`) chặn `..`, vì key dựng từ `sec_user_id` + `aweme_id`, **cả hai là dữ liệu Douyin**. Tầng thứ hai (`realpath` thư mục cha trong `put` và `remove`) chặn trường hợp một thư mục **trung gian là symlink** trỏ ra ngoài — dữ liệu từ xa không tạo được symlink, nên đây là ca `DATA_DIR` dùng chung / được mount, không phải ca Douyin. Thêm vì comment của guard hứa nhiều hơn guard làm được. `exists()` cố ý **không** trả phí này: nó không ghi, không xoá.
8. **`PARSER_BASE_URL` phải là URL http(s) có host** (`env.ts`). `z.string().url()` **không đủ**, và khoảng hở không hiển nhiên: `new URL("parser:80")` **thành công** (scheme `parser:`, path `80`), rồi ném `ERR_INVALID_URL` đúng lúc `ParserClient` resolve route lên nó — trong lane, dưới dạng exception mà **không** `catch (ParserError)` nào bắt. Kèm theo: `new URL(...)` đã được đưa **vào trong** `try` của client, để cả khi env validation bị nới lỏng thì cũng không có exception lạ nào thoát ra khỏi method.
9. **Integration suite tách khỏi `pnpm test`** sang `pnpm test:integration`. Suite db cũng cần docker nên "cần docker" không phải lý do; suite này cần **internet công cộng + Douyin sống + cookie chưa bị ban** — một hạng độ-tin-cậy khác, và nhét vào suite mặc định sẽ dạy mọi người nhún vai trước một lần chạy đỏ.
10. **Không thêm hook nào vào `Storage.put()`** — interface giữ đúng §2.2. Nhưng **hình dạng của thay đổi phase 6 đã được ghim thành comment ngay tại interface**, vì bản Outcome đầu nói nó "cộng thêm thuần tuý" và điều đó **sai**: khi `beforeCommit` trả `false` thì **không có `PutResult` nào để trả**, nên kiểu trả về phải nới thành `PutResult | null` hoặc phải throw — đằng nào cũng đụng mọi caller. Và một commit bị từ chối **vẫn phải unlink temp file**, trong khi cleanup hiện tại chỉ chạy trên đường throw; `if (!ok) return null` ngây thơ sẽ rò một file kích thước đầy đủ cho mỗi download bị reap.

### Thêm ngoài DoD

- **Test crash bằng `SIGKILL` thật, ở tiến trình riêng.** Một "crash mô phỏng" trong cùng process luôn chạy qua một nhánh cleanup nào đó của ta; `SIGKILL` không chạy nhánh nào cả. Đã kiểm bằng canary: thay thân `crash-writer.ts` bằng `process.exit(0)` ⇒ test **đỏ**, tức nó thật sự bị điều khiển bởi tiến trình con.
- **Harness `overlappingStreams`.** `Promise.all` trên hai `put()` chỉ chứng minh hai lời gọi được **phát đi** đồng thời; cái thứ nhất có thể xong hẳn — kể cả `rename` — trước khi cái thứ hai kịp mở file. Mỗi stream nhả một chunk, báo đã đến, rồi **từ chối đi tiếp** cho tới khi mọi stream khác cũng đã đến.
- **Hai negative control trong integration suite** (xem lệch 1).
- **Guard `it.failing` + banner stderr** cho điều kiện tiên quyết thiếu. `it.skip` đọc như "ổn"; `it.failing` giữ test và **bắt buộc** nó ném lỗi, nên không ai gỡ điều kiện tiên quyết trong im lặng. Jest vẫn vẽ dấu tích, nên banner mới là thứ làm gap hiện ra.
- **Forwarder TCP loopback** (`test/support/parser-forward.ts`). Parser cố ý **không** publish port nào — `compose-constraints.spec.ts` làm đỏ build nếu ai thêm `ports:`. Forwarder `python:3.11-slim` bind **chỉ loopback**, sống đúng trong thời gian suite chạy, để không phải làm yếu chính ràng buộc đó. Chọn `python:3.11-slim` vì máy nào build được image parser đều đã có nó; socat là thêm một lần pull.

### Một ngoại lệ có khai báo với §0 nguyên tắc 4

**`fsync` trước `rename` không có mutation test, và không thể có.** `rename` được sắp thứ tự với **metadata**; nội dung file có thể còn nằm trong page cache. Một host mất điện ngay sau `rename` có thể quay lại với file nằm đúng tên cuối, **rỗng hoặc rách bên trong** — đã commit, đã đánh dấu `done`, không có gì kích hoạt tải lại. Toàn bộ màn temp-file tồn tại để "committed" nghĩa là "complete"; không có dòng này thì nó nghĩa là "complete trừ khi máy chết", đúng ca nó sinh ra để chống.

Không test được vì không có cách nào mô phỏng mất điện trong jest, và một test spy vào `open()` chỉ khẳng định lại implementation. Ghi ra đây thay vì giả vờ nó được test. Thư mục cố ý **không** fsync: mất `rename` tốn một lần tải lại và reaper F3 đã lo; mất **nội dung** mới là hỏng hóc không có ai phát hiện.

### Gap — chưa đóng, không được coi là đã xong

1. **Short link `v.douyin.com/xxx/` chưa verify bằng link sống.** Douyin cấp link này theo từng lượt share, không bịa được. Phía *client* đã xong phần của nó (3 test khẳng định URL được chuyển tiếp **nguyên văn**, không một dòng parse URL nào ở phía Node); phía *fork* xử lý nhánh này ở `crawlers/douyin/web/utils.py:337`. Thiếu đúng một lần chạy end-to-end: đặt `DOUYIN_TEST_SHORT_LINK` rồi `pnpm test:integration`.
2. **Môi trường hiện tại không có cookie Douyin nào** (`douyin:cookie:current` rỗng, `DOUYIN_COOKIE_FALLBACK` rỗng), nên mọi route trừ `get_sec_user_id` chỉ trả 400 catch-all. Đây là **F4 quan sát được ngoài đời**: hai route khác nhau, cùng một lỗi 400 y hệt, nguyên nhân thật (không có cookie) hoàn toàn vô hình. Assertion dữ liệu thật vì thế bị gate.
3. **Fault injection `__TEST_ONLY_SIMULATE_BOOTSTRAP_ERROR__` trong `main.ts` VẪN CÒN.** Phase 2 **không** wire client nào vào bootstrap: `ParserClient` và `Storage` do lane dựng, không phải `NestFactory`. Test D-3 cần một lỗi bootstrap **có nhúng DSN**, chỉ lỗi kết nối DB/Redis sinh ra. Dời sang **phase 3**.
4. **`.tmp` mồ côi chưa có ai dọn.** `SIGKILL` giữa chừng để lại `{key}.{uuid}.tmp` vĩnh viễn — test khẳng định đó là hành vi **đúng** (không commit gì), nhưng không code nào xoá nó. Qua một vòng crash-loop chúng tích lại ở kích thước video đầy đủ, `exists()` không thấy, mà vẫn ăn phần đĩa `freeBytes()` hứa với preflight. **Đã giao cho reaper phase 4** trong risk register.

### Env test-only

Không phải config service, `env.ts` không validate: `DOUYIN_TEST_SHORT_LINK` · `DOUYIN_TEST_PROFILE_URL` · `DOUYIN_TEST_SEC_USER_ID` · `DOUYIN_TEST_AWEME_ID` · `PARSER_TEST_NETWORK` · `PARSER_TEST_HOST`.
