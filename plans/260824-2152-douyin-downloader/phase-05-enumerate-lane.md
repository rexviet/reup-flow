---
phase: 5
title: Enumerate lane — global 1 luồng
status: completed
effort: ~0.5d
blockedBy: [4]
milestone: M1
---

# Phase 5 — Enumerate lane

Đọc trước: solution design §5.2 (sequence) · `plan.md` §2 (F2) · IDEA.md §3.1 §6.2.

---

## 5.1 F2 — concurrency = 1 **toàn hệ thống**, không phải 1 mỗi job

**Rate limit của Douyin theo IP, không theo job.** 3 người bấm 3 job = 3× tần suất request = ban. Job thứ 2 **xếp hàng chờ**.

Hiện thực: **đúng một** instance `EnumerateLane` được đăng ký. Không phải semaphore, không phải config — là số lượng loop. Cấu trúc bảo đảm, đây là lý do chọn A1.

## 5.2 Vòng lặp

```
claim 1 job (SKIP LOCKED) + lease 10m
cursor = job.max_cursor ?? 0
loop:
  page = parser.fetchUserPostPage(sec_user_id, cursor, count=20)

  if page.awemeList rỗng && page.hasMore == 1:
      → BLOCKED (xem 5.3), giữ nguyên checkpoint, dừng

  upsert videos ON CONFLICT (aweme_id) DO NOTHING
        kèm play_addr + url_expires_at = now + URL_TTL_SECONDS   ← F1
  job.enumerated_count += page.awemeList.length
  job.max_cursor = page.maxCursor          ← CHECKPOINT MỖI TRANG
  heartbeat lease
  if !page.hasMore: break
  sleep(3–5s + jitter)

job.total_videos = count(videos WHERE sec_user_id)
job.status = 'downloading', nhả lease
```

**Checkpoint mỗi trang, không phải cuối vòng lặp.** Crash giữa job thì resume từ trang cuối đã lưu.

**`sleep` nằm TRONG vòng lặp, giữa các trang.** Đây chính là yêu cầu mà `@Interval` không đặt đúng tầng được — lý do #1 chọn A1 (solution design §4).

**Lưu `play_addr` ngay lúc enumerate (F1)** là toàn bộ chỗ tiết kiệm: profile 500 video tốn ~25 request thay vì 525.

## 5.3 Phát hiện `blocked` — phase này CHỈ dùng tín hiệu cấu trúc

`awemeList` rỗng **nhưng** `hasMore == 1` → cookie bị chặn. Đây là HTTP **200** và là chẩn đoán **chắc chắn** — dùng ngay, không cần probe.

→ `status = 'blocked'`, **giữ nguyên `max_cursor`**, dừng. **Không phải `failed`.**

**Xử lý HTTP 400 để sang phase 7.** Ở phase 5, 3× 400 liên tiếp → dừng job ở `blocked` **tạm thời** kèm comment `TODO(phase-7)`. Lý do không đoán bây giờ: parser bóp mọi lỗi thành 400 vô nghĩa (F4), nên đoán ở đây sẽ báo `blocked` cho profile private và người vận hành đi đổi cookie vô ích.

---

## Todo

- [x] `enumerate.lane.ts`, **đúng 1 instance** (`lanes.module.ts`, `LANES` factory)
- [x] Claim job + lease + heartbeat mỗi trang (`checkpoint` renew lease cùng statement)
- [x] Upsert `ON CONFLICT DO NOTHING` kèm `play_addr` + `url_expires_at` (`videos.upsertMany`)
- [x] Checkpoint `max_cursor` **mỗi trang**
- [x] `sleep(min–max + jitter)` giữa trang, **cuối thân loop** nên không sleep sau trang cuối
- [x] Blocked theo tín hiệu cấu trúc + 3× 400 (`markBlocked`, giữ `max_cursor`)
- [x] `total_videos` + chuyển `downloading` (`finishEnumerate`, đếm bằng SQL từ profile)
- [x] `aweme.mapper.ts` — drop item không có `aweme_id`/`create_time`, null cột còn lại

## DoD

- [x] Profile ~30 video: enumerate xong, `total_videos` khớp số row
- [x] Kill giữa enumerate → restart → tiếp từ `max_cursor`, **không quét lại từ 0**
- [x] Fake parser trả `awemeList=[]` + `hasMore=1` → `blocked`, `max_cursor` **không đổi**
- [x] Chạy lại profile đã enumerate → 0 row mới (`ON CONFLICT` hoạt động)
- [x] `play_addr` + `url_expires_at` có giá trị **ngay sau** enumerate
- [x] **Mutation:** đăng ký 2 instance lane → test tần suất 2 job **đỏ**
- [x] **Mutation:** chuyển checkpoint ra sau vòng lặp → test resume **đỏ**
- [x] **Mutation:** map `blocked` → `failed` → test hồi phục cookie **đỏ**

---

## Outcome

`pnpm test` 516/516 xanh (46 suite) · `pnpm lint` sạch ·
`bash test/mutations/phase-05.sh` exit 0: **32 mutation must-fail đều đỏ, 2 control đều xanh**.

### Review đối kháng: 4 assertion **không thể đỏ** + 1 guard không có test nào

Review chạy mutation để *chứng minh* từng cái, không phải đoán. Cả 5 đều đã sửa và giờ đều
có mutation canh:

| Chỗ | Vì sao không thể đỏ | Sửa |
|---|---|---|
| Test jitter | Inject `random: () => 0` ⇒ luôn lấy **sàn**: nó *tạo ra* metronome rồi assert metronome nằm trong khoảng. Thay jitter bằng hằng số 3000ms ⇒ **vẫn xanh** | So các lần rút **với nhau** (`new Set(delays).size > 1`) trên 6 trang |
| Test rò log | Chỉ assert vắng `sessionid`/`passport_csrf_token` — cookie **không bao giờ** trong scope của lane (nó ở fork, đọc từ Redis) ⇒ không mutation nào của file này phá được. Mà `sec_user_id` trong chính tiêu đề thì **không** được assert | `not.toContain(PROFILE)` — `sec_user_id` là thứ *thật sự* lane giữ |
| Đếm item bị drop | `toMatch(/2/)` — bất kỳ số `2` nào trong log đều thoả, thực tế là **UUID của job**, ~7/8 lượt. Xoá hẳn câu log ⇒ vẫn xanh | `toMatch(/2 item\(s\) dropped/)` |
| Nửa "hành vi" của `blocked` ≠ `failed` | Assert `toBe('blocked')` **trước** khi gọi `cancel()`, nên dưới đúng mutation nó sinh ra để bắt, test chết ở dòng đó và **không bao giờ chạy tới** phần cancel ⇒ file chỉ còn 2 assertion về *cái từ* | Gọi `cancel()` **trước** mọi assertion về status + mutation riêng bỏ `blocked` khỏi `CANCELLABLE_STATUSES` |
| Pacing trên đường retry 400 | **Không có test nào** — mọi assertion về khoảng cách đều dùng script không có 400. Xoá `pause()` ở nhánh này ⇒ 33/33 xanh | Thêm assertion khoảng cách trên script có 400 + mutation |

Cái cuối là lỗ thật, không phải chỉ thiếu mutation: không có `pause()` đó thì lane bắn 3
request liền nhau, park job, rồi vì tick vẫn báo `'worked'` nên quay lại lấy job kế tiếp và
làm lại — `3 × độ sâu queue` request không giãn vào endpoint rate-limit **theo IP**, đúng cái
ban F2 sinh ra để tránh, đi qua đúng con đường mọi test pacing không phủ.

### Hai guard được thắt lại sau review

**Cursor phải GIẢM, không chỉ "khác".** Guard cũ chỉ chặn cursor **bằng** cursor hiện tại.
Hình dạng nguy hiểm hơn vẫn hở: cursor **tăng** thì đi ngược về video mới nhất, trang 1 quay
lại, lane dao động vĩnh viễn — lease renew mỗi trang, `enumerated_count` tăng, một request
mỗi 3–5s vào ngân sách IP, và `runOnce` **không bao giờ return** nên queue phía sau cũng đứng.
Chặn được là vì hướng cursor **giờ đã biết** — chính sự thật mà `checkpoint` dựa vào, dùng lần
thứ hai. (`cursor === 0` là ngoại lệ duy nhất: đó là cách **mọi** job bắt đầu.)

**`checkpoint` trả `false` khi nó **loại** bản ghi.** Bản đầu loại cursor của straggler nhưng
vẫn trả `true` — tức bảo lane vừa bị vượt qua rằng "vẫn của mày, cứ chạy": **hai luồng
request song song** vào endpoint rate-limit theo IP, tồn tại đúng bằng thời gian straggler
sống. Đó là F2 bị vi phạm bởi chính đoạn code bảo vệ checkpoint. Predicate đã được tính sẵn
cho SET nên chỉ cần đưa thêm vào WHERE, và nhánh `if (!stillOurs) return 'lost'` có sẵn của
lane dừng straggler ngay. Hệ quả: mọi checkpoint phải mang **nhiều item hơn hẳn**, nên lane
không được checkpoint trang rỗng — nó `break` trước, vì trang rỗng chỉ có thể là hết profile
(rỗng + `hasMore` đã là tín hiệu `blocked`).

Thêm `Math.trunc` cho cursor: `max_cursor` là bigint, mà số của parser nhận cả phi nguyên, nên
`...600000.5` tới thẳng parameter và Postgres **từ chối cả statement**. Tick fail an toàn rồi
lặp lại mãi, log không hề nhắc tới dấu thập phân.

Và `release()` ở nhánh throw giờ có `.catch` riêng: không có nó, một release lỗi (Postgres
sập — lý do khả dĩ nhất) **thay thế** lỗi parser đã gây ra fail, nên log gọi tên triệu chứng
bậc hai và mất nguyên nhân bậc một.

### Review khẳng định lại (đã kiểm bằng cách chạy, không phải đọc)

- **`checkpoint` mới đúng**, không dựng được interleaving nào mất dữ liệu: hai nhánh SET dùng
  **chung một predicate** nên row luôn giữ cặp `(count, cursor)` nhất quán của **một** writer,
  và lane chỉ ghi cursor **sau khi** `upsertMany` đã commit trang đó — nên cursor thắng luôn
  có insert phủ tới nó. Tie thì thắng theo thứ tự đến, mất mát bị chặn trong khoảng giữa hai
  lần fetch và **không phải mất dữ liệu** (trang sau fetch lại, dedupe).
- **Không có SQL injection**: bật `log_statement='all'` rồi đọc statement thật — mọi nội suy
  đều là placeholder, kể cả cái mang `::bigint`.
- Positive control của F2 là **thật**: `inFlight` tăng đồng bộ lúc vào, giảm trong `finally`
  sau 80ms, nên số 2 quan sát được. (Kèm sửa: `reset()` phải reset cả `inFlight`, nếu không
  một request còn bay lúc reset làm `maxInFlight === 2` thoả **mà không có overlap nào** —
  đúng assertion duy nhất có việc chứng minh dụng cụ đo *có thể* sai lại thành không thể sai.)
- `enumerated_count` **một writer duy nhất** (chỉ `checkpoint`) — đã ghi vào comment, vì
  writer phase 7 nào reset nó sẽ đóng băng cursor lại y như bug vừa sửa, từ đầu bên kia.

### Bug PHASE 4 SHIPPED, phase 5 tìm ra — `checkpoint` dùng `greatest()` trên `max_cursor`

Phase 4 chống "straggler rewind" bằng `max_cursor = greatest(cũ, mới)` — đơn điệu theo
**giá trị số** của cursor. Đó là một giả định về cách Douyin phân trang mà Douyin không
tôn trọng: `aweme/post` trả `max_cursor` = `create_time` (ms) của item **cũ nhất** trong
trang, và profile phân trang **mới nhất trước**, nên **cursor GIẢM mỗi trang**.

Hệ quả dưới `greatest`: checkpoint **đóng băng ở trang 1** suốt cả job, trong khi
`enumerated_count` vẫn tăng và lease vẫn renew — **mọi thứ quan sát được đều lành mạnh** —
và mỗi lần restart lại quét từ trang 2, đúng ngân sách request F2 mà checkpoint sinh ra để
bảo vệ.

**Không test nào trong repo bắt được**, vì fake parser tự do trả số tăng dần. Đây chính là
hình dạng false-green mà repo đã gặp 4 lần: test xanh vì fixture dễ hơn thực tế.

**Sửa:** đổi *thước đo* chứ không đổi *invariant*. Cursor chỉ được ghi khi
`enumerated_count` đến **lớn hơn hẳn** giá trị đang lưu. `enumerated_count` chỉ tăng, bất
kể cursor đi hướng nào, nên "nhiều item hơn" = "đi xa hơn" theo định nghĩa; straggler thấy
**ít** item hơn nên vẫn không rewind được gì. Strictly greater, không phải `>=`: hai lane
cùng count thì không có thứ tự giữa chúng. Test giờ phân trang **giảm dần** như thật, và có
mutation đặt `greatest()` trở lại — đo cả ở tầng repository và **qua lane**.

### Claim sai về Postgres, bắt được bằng mutation stayed-green

`upsertMany` ban đầu dedupe item trùng trong một trang bằng `Map`, kèm comment nói
`ON CONFLICT` sẽ raise "cannot affect row a second time". Mutation phá `Map` **vẫn xanh** ⇒
comment sai. Đo thật: `INSERT ... VALUES ('a',1),('a',2) ON CONFLICT DO NOTHING` trả 1 row,
**không lỗi**; chỉ `DO UPDATE` mới raise. `Map` là dead code ⇒ xoá, comment viết lại theo
số đo, và mutation vô nghĩa đó bị loại khỏi harness. Đây là lý do
"mutation stayed green" phải là **lỗi cứng**: nó là kênh duy nhất báo rằng một comment
đang nói dối.

### `blocked` ≠ `failed` — test hiển nhiên là false green

`RETRYABLE_STATUSES = ['blocked', 'failed']`, nên một test "hồi phục cookie" chỉ đi hết
luồng (kẹt → push cookie → retry → xong) **vẫn xanh** khi map `blocked` → `failed`. Nó trông
như bằng chứng end-to-end mạnh và không chứng minh gì về chính sự phân biệt nó mang tên.
Phải neo vào hai chỗ *thật sự* quan sát được hôm nay: (1) chính **cái từ** — cả hai đều
retryable nên status **là** chỉ dẫn remediation; (2) `CANCELLABLE_STATUSES` chứa `blocked`
và **loại** `failed`, nên job `blocked` vẫn cancel được — đó là hệ quả hành vi của cái từ,
không phải cách nói lại nó. Kèm **control phải-xanh**: đổi lời log/prose ⇒ suite vẫn xanh,
chứng minh test neo vào status chứ không neo vào văn bản.

### Guard thêm ngoài spec

| Guard | Vì sao |
|---|---|
| Cursor không tiến ⇒ **throw** | `has_more` + cursor y nguyên ⇒ request kế tiếp giống hệt request vừa rồi: loop vô hạn tiêu F2 ở tốc độ tối đa, mà **mỗi trang đều là HTTP 200 lành mạnh** |
| Chỉ 400 mới được retry/đếm về `blocked` | 404/422 là bug hợp đồng phía mình; báo `blocked` cho nó là đẩy người vận hành đi đổi cookie vô ích — sai F4 từ hướng ngược lại |
| `aweme` thiếu `create_time` ⇒ **drop item** | `create_time` NOT NULL **và** là thứ tự claim download; default `now()` đưa video 2019 lên đầu queue và **không chỗ nào downstream báo là sai** |
| Tick throw ⇒ nhả claim rồi mới rethrow | Nhả để tick sau retry ngay thay vì chờ hết `LEASE_SECONDS`; rethrow để base class log + backoff, parser sập không thành hot loop |
| `finishEnumerate`/`markBlocked`/`releaseClaim` đều gate `status='enumerating'` | Lane bị reap giữa đường không được ghi đè row giờ thuộc người khác |

**Cửa sổ còn hở, ghi ra chứ không giả vờ đã đóng:** nếu row bị reap **rồi** bị claim lại,
status lại là `enumerating` và ba hàm trên không phân biệt được claim mới với claim cũ.
Đóng đúng cách cần ownership token — `lane.runner.ts` đã gán việc đó cho phase 6 (kiểm lease
trước `rename`); token đó thuộc về đây khi nó có. Hôm nay cửa sổ bị chặn bằng *số học*
(drain tính bằng giây, lease 300s) và **số học không phải là guard**.

### Ghi chú harness

`test/mutations/phase-05.sh` mang cả 6 luật của plan §5. Hai lần sập khi viết, cả hai đều
là *harness tự nói dối*, đúng như hệ quả §5 dự báo: (a) backtick trong nhãn mutation mở
command substitution ⇒ bash chết giữa run, 17 mutation đầu đã chạy nhưng script không tới
được `restore` của chính nó (trap pristine của luật 5 đã cứu cây nguồn); (b) fake
`PARSER_CLIENT` thiếu `close()` ⇒ `ParserModule.onApplicationShutdown` throw, pg pool +
Redis không đóng, suite **treo không in gì** thay vì fail.

## Risks

| Risk | Xử lý |
|---|---|
| Ai đó "tối ưu" thành enumerate song song nhiều job | Mutation test tần suất. Comment ghi rõ rate limit theo IP |
| Đoán nguyên nhân từ 400 ngay ở phase này | Chỉ dùng tín hiệu cấu trúc. 400 để phase 7 |
| `sleep` bị hoisted ra ngoài loop | Test đo khoảng cách giữa các request, không chỉ tổng thời gian |
