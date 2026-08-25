---
phase: 7
title: "Durability: F4 probe, retry/cancel, bộ mutation test"
status: completed
effort: ~1.5d
blockedBy: [6]
milestone: M2
---

# Phase 7 — Độ bền · **kết thúc M2**

Phase nặng nhất. Đây là chỗ hệ thống chuyển từ "chạy được" sang "chạy được khi mọi thứ đổ vỡ".

Đọc trước: solution design §1.5 (F4) · §5.2 đường lỗi · `plan.md` §5 (bảng mutation).

---

## 7.1 F4 — probe disambiguation

**Vấn đề.** `app/api/endpoints/douyin_web.py` bắt `except Exception` và trả `ErrorResponseModel` với `message` **mặc định tĩnh** `"An error occurred."`. `str(e)` không bao giờ được truyền vào. Nên bốn nguyên nhân này **giống hệt nhau** từ phía NestJS:

| Nguyên nhân thật | Remediation đúng |
|---|---|
| Cookie bị ban | Push cookie mới → `blocked` |
| `a_bogus` lỗi thời | Pull upstream |
| Profile private / đã xoá | `failed`, thông báo user |
| Parser không ra được Douyin | Retry, chờ mạng |

Làm đúng IDEA.md §6.2 ("3 lỗi liên tiếp → `blocked`") sẽ **báo `blocked` cho profile private**, và người vận hành đi đổi cookie vô ích.

**Giải pháp — ba tầng tín hiệu, không patch parser:**

1. **Tín hiệu cấu trúc** (đã làm ở phase 5): `awemeList` rỗng + `hasMore=1` → `blocked`. HTTP 200, chẩn đoán **chắc chắn**, không cần probe.
2. **3× HTTP 400 liên tiếp → chạy probe**: `fetchUserProfile(PROBE_SEC_USER_ID)` — một profile public đã biết, đặt trong env.
   - probe **fail** → lỗi **toàn cục** (cookie / `a_bogus` / mạng) → `blocked`
   - probe **OK** → riêng profile này → `failed`, `error = "profile không truy cập được, cookie vẫn tốt"`
3. Probe **chỉ chạy trên đường lỗi** ⇒ không đụng ngân sách rate limit đường thường.

**Không patch parser để lấy error text** — sẽ đốt ngân sách `check-patch-size.sh` vào file upstream sở hữu, đúng loại nợ mà `PATCH_NOTES.md` được viết ra để tránh (`plan.md` §0 nguyên tắc 2).

Thay `TODO(phase-7)` ở phase 5 bằng cái này.

## 7.2 Vòng hồi phục đầy đủ

Chuỗi phải chạy end-to-end (solution design §5.4):

```
cookie ban → blocked, max_cursor giữ, videos done vẫn done
  → push cookie mới → parser dùng ngay, KHÔNG restart
  → POST /retry → blocked→pending, videos failed→pending, attempts=0
  → lane tiếp TỪ max_cursor, không làm lại
```

## 7.3 Rate limit test đúng từng pha

- **Enumerate:** 2 job đồng thời → đo tần suất request tổng == 1 luồng (F2)
- **Download:** N video đồng thời → đo có đúng `DOWNLOAD_CONCURRENCY` request bay cùng lúc, không hơn

Hai pha **rate limit khác nhau hoàn toàn** (IDEA.md §3.1). Áp cùng một giới hạn cho cả hai là chậm hơn cần thiết cả chục lần.

## 7.4 Bộ mutation test — bàn giao đủ **14 invariant** ở `plan.md` §5

> Spec gốc viết "10 mutation". Bảng §5 đã lên **14 invariant** (phase 6 thêm ba dòng: kiểm
> lease trước `rename`, verify `content-length`, file 0 byte). Con số dưới đây là con số
> thật, không phải con số spec viết lúc chưa có ba dòng đó.

**Luật thi hành:** mỗi mutation phải **in ra diff nó thật sự áp được**. Mutation match rỗng ⇒ **coi như chưa chạy**, không phải "đã pass". Phase 1 sập bẫy này hai lần: `sed` mutation match rỗng sau refactor nên "suite vẫn xanh" là mutation tồi chứ không phải gap thật.

Đưa vào script `scripts/run-mutations.sh`, chạy trong CI. Không để trong đầu người.

---

## Todo

- [x] Probe disambiguation (F4) — `src/parser/f4-probe.ts` (file riêng, không nhét vào `parser.errors.ts`: chỗ đó là *lớp lỗi*, còn đây là *quyết định*)
- [x] `PROBE_SEC_USER_ID` vào env + zod — đã có từ phase 5; phase 7 thêm `ProbeHealthCheck` canh nó lúc boot
- [x] Đếm 400 liên tiếp per job, reset khi thành công — có test *cả hai chiều* + mutation
- [x] Thay `TODO(phase-7)` ở enumerate lane (và ở `isProfileCancelled`)
- [x] `/retry` reset `attempts=0` + `blocked→pending`, **và** `cancelled→pending` + hồi sinh video `skipped`
- [x] Cancel giữa lúc tải: abort stream, dọn `.tmp` (phase 6 đã làm; phase 7 thêm guard chống *stranded* `skipped`)
- [x] Test rate limit 2 pha — enumerate (phase 5) + **download đo chồng lấn thật** (mới)
- [x] `scripts/run-mutations.sh` — **cả 7 harness**, in diff từng cái, manifest số lượng
- [x] CI chạy `scripts/run-mutations.sh` (thay vì chỉ `phase-01.sh`) + thêm service `redis`

## DoD

- [x] Fake parser 400 + probe **OK** → `failed` kèm lý do rõ, **KHÔNG** `blocked`
- [x] Fake parser 400 + probe **fail** → `blocked` (ba nguyên nhân fail phân biệt được bằng fixture)
- [x] Vòng hồi phục §7.2 chạy đủ, không tải lại video đã done — đo bằng **request thật trên dây**
- [x] 2 job enumerate đồng thời → tần suất == 1 luồng (phase 5, vẫn xanh)
- [x] Download đồng thời == `DOWNLOAD_CONCURRENCY`, không hơn — đo **in-flight thật**, kèm positive control thấy được số lớn hơn
- [x] **Cả 14 invariant ở `plan.md` §5 đã chạy, mỗi cái in diff áp được, mỗi cái đỏ**
- [x] `scripts/run-mutations.sh` chạy trong CI, fail build nếu mutation nào **không** đỏ
- [x] Fail build nếu mutation nào áp **rỗng** — và nếu harness **thiếu kết quả** so với manifest, hoặc để lại cây nguồn đã đổi

## Risks

| Risk | Xử lý |
|---|---|
| `PROBE_SEC_USER_ID` trỏ profile đã chết → probe luôn fail → mọi thứ thành `blocked` | `ProbeHealthCheck` chạy probe một lần lúc bootstrap. **Lệch có chủ ý: to tiếng, KHÔNG fail fast** — check fatal sẽ làm cả dịch vụ không boot mỗi khi mạng chớp, mà đó là hại lớn hơn hại nó ngăn. Xem §Outcome |
| Probe đốt rate limit | Chỉ chạy sau 3× 400. Có test đếm |
| Mutation script match rỗng mà vẫn báo pass | Script **fail** nếu diff rỗng. Đây là bài học đắt nhất của phase 1 |
| **Harness ruỗng mà không ai biết** (phase-04 mất 2 mutation suốt 5 phase) | `scripts/run-mutations.sh` đối chiếu **manifest số lượng** từng harness + checksum cây nguồn trước/sau. Xem §Outcome mục D |
| Đếm 400 không reset khi thành công | Job dài sẽ tích đủ 3 lỗi rải rác rồi báo blocked sai. Có test |

---

## Outcome

`pnpm test` **669/669 xanh (62 suite)** · `pnpm lint` sạch.

`bash scripts/run-mutations.sh` — **cả 7 harness, 135 mutation must-fail + 15 control
must-stay-green**, mỗi cái in diff áp được:

```
phase-01  OK  caught=8/8   controls-green=0/0  empty=0 no-tests=0 hung=0
phase-02  OK  caught=15/15 controls-green=1/1  empty=0 no-tests=0 hung=0
phase-03  OK  caught=18/18 controls-green=2/2  empty=0 no-tests=0 hung=0
phase-04  OK  caught=20/20 controls-green=2/2  empty=0 no-tests=0 hung=0
phase-05  OK  caught=32/32 controls-green=2/2  empty=0 no-tests=0 hung=0
phase-06  OK  caught=14/14 controls-green=3/3  empty=0 no-tests=0 hung=0
phase-07  OK  caught=28/28 controls-green=5/5  empty=0 no-tests=0 hung=0
```

Live, trên stack vừa `docker compose up -d --build` (việc phase 8 và 9 bị chặn không làm được):
`GET /api/jobs` 200 · `GET /internal/cookie/status` 200 · `POST /retry` trên job `blocked` trả
`videos_reset: 1`, giữ nguyên `max_cursor = 1735689600000`, video `skipped` về `pending`/`attempts=0` ·
`POST /retry` trên job **`cancelled`** trả 200 (trước phase 7 là 409) · và log boot in đúng dòng
`F4 PROBE TARGET IS NOT USABLE ...` vì máy này không ra được Douyin — tức guard mới hoạt động
**và** hoạt động theo kiểu không chặn boot.

### F4: năm outcome, không phải hai

Spec viết probe fail → `blocked`, probe OK → `failed`. Nhưng "probe fail" gộp ba nguyên nhân có
**ba remediation khác nhau**, và bảng §7.1 liệt kê đúng ba cái đó. `plan.md` §5 **luật thứ chín**
nói thẳng: nếu fake parser không dựng được cả bốn nguyên nhân thì test F4 không chứng minh gì.
Nên `probeCookie` trả **năm** verdict, mỗi cái một reason riêng, và fixture dựng được cả năm:

| Verdict | Fixture | Status | Reason nói gì |
|---|---|---|---|
| `cookie-healthy` | 200, `status_code = 0` | **`failed`** | cookie vẫn tốt, **đừng** đổi cookie |
| `cookie-refused` | 200, `status_code = 2190` | `blocked` | push cookie mới |
| `parser-broken` | throw `ParserRejectedError` | `blocked` | `a_bogus` cũ / bug crawler → pull upstream |
| `parser-unreachable` | throw `ParserUnavailableError` | `blocked` | chờ mạng, không đổi gì |
| `inconclusive` | 200, `status_code = null` | `blocked` | **không** khẳng định gì |

Dòng cuối là dòng đáng nhất. `parser.client.ts` từ phase 2 đã từ chối default `status_code`
thiếu về `0` ("inventing success that was never observed is the same mistake as inventing a cause
for a 400"). Phase 7 là chỗ lời từ chối đó **có tải**: verdict `failed` là verdict *ghi sổ* một
profile, nên nó đòi bằng chứng **dương**. Có hai mutation riêng cho hai đầu — một ở `f4-probe.ts`,
một ở `parser.client.ts` — vì mutation ở file này không bao giờ bắt được lỗi ở file kia.

Và HTTP 200 **không** có nghĩa Douyin đồng ý: fork dán envelope `code: 200` của nó lên bất cứ
thứ gì Douyin trả, nên một cookie bị ban về tới đây trông như một exchange hoàn toàn khoẻ mạnh —
chỉ `status_code` nói khác. Guard đó cũng có mutation.

### Ba lỗ do review đối kháng tìm ra — cả ba đều **không có invariant nào canh**

Phase 6 để lại một câu hỏi: *"đường lỗi nào không có guard?"*. Hỏi nó ở cuối phase 7 ra ba cái,
và cả ba đều không làm status ở đâu trông sai — đúng lý do sẽ không ai để ý.

**1. Probe mở ra một cửa sổ mà `park`/`giveUp` không nhìn.** Probe là network call 30s và **không**
renew lease. `markBlocked`/`markFailed` đều gate trên `status = 'enumerating'`, nên trong lúc chờ
probe, reaper có thể trao job cho người khác và cái ghi sau đó **không đụng row nào** — trong khi
code phase 5 vẫn log `job X is BLOCKED` về một job đang `pending` dưới claim mới. Một dòng log
khẳng định *ngược lại* điều đã xảy ra thì tệ hơn không log. Giờ cả hai writer trả boolean, và
`diagnose` trả `'lost'` + nói ra khi ghi không xuống.

**2. `ProbeHealthCheck` giữ bootstrap 30s.** Nest **await** `onApplicationBootstrap`, nên await
probe ở đó sẽ giữ `app.init()` mở suốt timeout của parser khi parser sập — chặn `/health`, và qua
`depends_on: service_healthy` chặn luôn container `web`. Check này được làm **không fatal** đúng
vì không muốn Douyin không tới được thì dịch vụ không chạy; block boot là trả lại phần lớn cái
hại đó bằng cửa sau. Giờ hook start rồi return ngay, và expose `done` để test còn xác định được
(test đo bằng một probe **không bao giờ** settle: hook vẫn phải return).

**3. Race mà việc hồi sinh `skipped` một mình không đóng được.** Đây là bug phase 6 dựng lại từ
một race chứ không từ một reset thiếu:

```
1. job A bị cancel
2. lane claim video V  → row thành `downloading`
3. job B được tạo. reviveSkippedForProfile chạy trong transaction đó và
   KHÔNG thấy gì: V đang `downloading`, không phải `skipped`
4. lane ghi `skipped`. ownsClaim thoả, nên nó xuống
```

V giờ `skipped` **dưới một job đang sống**. `claimOne` không bao giờ nhìn `skipped`, mà `skipped`
tính là **đã settle**, nên job B đi tới `completed` với `downloaded_count` thiếu V và **mọi con số
đều khớp**. Hai lần đọc `isProfileCancelled` mà lane đã có đều không đóng được: không cái nào
atomic với cái ghi này. Sửa bằng cách đưa câu hỏi vào **WHERE**: `markSkipped` chỉ xuống khi
**không** có job sống nào cho profile. Có job rồi thì nó không đụng row nào, lane coi claim là
lost, row nằm ở `downloading` cho reaper trả lại (F3) — chậm nhất một lease, thay vì mất video.

**4. `ProbeHealthCheck.done` reject vào hư không.** Không có gì `await` nó ở production (nó tồn
tại để test xác định được), nên một reject ở đó là **unhandled rejection** — tức process chết, vì
một chẩn đoán được thiết kế để *không* fatal. `run()` đã bắt mọi lỗi probe, nên đường còn lại là
chính logger ném — đúng finding thứ 4 của review phase 4: *lớp phòng thủ ngoài cùng không được
phép là thứ vỡ*. Có `.catch()` + test với logger phát nổ + mutation.

### Hai lần chính bộ mutation bắt tôi

Đáng ghi lại vì nó là bằng chứng harness đang làm việc chứ không chỉ chạy:

**Một test của tôi không thể đỏ.** Test "claim bị lấy giữa lúc probe" ban đầu chỉ assert *trạng
thái DB*: `status === 'pending'`, `error === null`. Cả hai **đúng dù có fix hay không** — cái ghi
gate trên `status = 'enumerating'` nên bỏ qua câu trả lời của nó không đổi row nào. Thứ duy nhất
khác là lane **nói gì**, và toàn bộ defect là một dòng log khẳng định `job X is BLOCKED` về một
job đang `pending` dưới claim của người khác. Mutation "verdict writers ignore whether the write
landed" **vẫn xanh** ⇒ test vô nghĩa. Giờ log **là** assertion.

**Và một mutation của chính phase 7 bị `matched nothing` ngay trong phase 7** — sau khi fix
finding #4 đổi `this.done = this.run()` thành `this.done = this.run().catch(...)`, anchor của
mutation "boot check blocks bootstrap" chết. Đây đúng là hình dạng phase-04 bị ruỗng dưới refactor
của phase 5, chỉ khác là lần này nó **fail build ngay trong cùng một lượt chạy** thay vì sống sót
qua năm phase. Đó là cả lý do `run-mutations.sh` tồn tại.

**Một guard đo bằng *thời gian* thay vì bằng *giá trị*.** Test "không chặn bootstrap" ban đầu
kiểm `healthy` vẫn `null` — mà một hook **await** một probe không bao giờ settle cũng thoả điều
đó: nó **treo** chứ không đỏ, và treo không nêu tên gì (luật 6). Nest await bất cứ thứ gì hook
trả về, nên "không chặn bootstrap" *chính là* "không trả về thứ gì để await". Giờ assertion là
`expect(returned).toBeUndefined()`.

Và cùng đường đó bắt thêm một chuyện: dưới mutation ấy, test logger-phát-nổ **cùng file** nhận
một promise **reject** từ một hook nó không await ⇒ unhandled rejection ⇒ Node giết worker jest ⇒
cả suite không ra kết quả nào. Harness từ chối đọc đó là "caught" (luật 2/8), đúng. Sửa: mỗi
mutation một `-t` riêng.

**Hai mutation bị bắt bằng cách TREO, không bằng cách đỏ** (luật 6). Dưới mutation đổi độ dài mảng
lane, assertion sanity trong `beforeAll` của `download-concurrency.spec.ts` ném **trước khi** biến
`close` được gán, nên Nest app không bao giờ đóng, pg pool ở lại, và jest treo 300s. Harness báo
"caught, but by HANGING" — một catch hợp lệ mà **không nêu tên gì**. Sửa: gán `close` **trước**
assertion (teardown chỉ chạy ở đường thành công thì không phải teardown), và hạ ceiling của barrier
từ 1500ms xuống 400ms — nó chỉ bị trả khi hệ thống *tuần tự hơn nó khai*, và trả một lần mỗi
request, nên số lớn biến một lỗi rõ ràng thành một timeout.

### Bàn giao phase 6, món 1: `skipped` không có đường về

Hai đường về, vì chúng trả lời **hai ý định khác nhau** của người vận hành, và mỗi đường có control
riêng chứng minh nó **đứng được một mình** (§5 luật 4):

- `/retry` trên chính job đã cancel → `cancelled` vào `RETRYABLE_STATUSES`.
- Tạo **job mới** cho profile → `createFromUrl` gọi `reviveSkippedForProfile` **trong cùng
  transaction** với insert, nên không có cửa sổ nào tồn tại một job sống bên cạnh video còn
  `skipped` (một lane tick trong cửa sổ đó sẽ không thấy việc gì làm và có thể finalize luôn job
  vừa được tạo ra để chạy nó).

`resetFailedForProfile` **không** bị nới rộng ra để gộp `skipped`. `failed` là "đã thử và không
được", `skipped` là "không có gì sai, không ai cần" — gộp lại thì còn một hàm mà cái tên chỉ đúng
một nửa việc nó làm, đúng loại comment-nói-dối §5 luật 7 nói tới. Hai hàm, hai ý, `videosReset`
cộng cả hai vì con số đó là thứ API trả về làm *xác nhận*.

### Bàn giao phase 6, món 2: harness ruỗng phải fail build

`scripts/run-mutations.sh` làm bốn việc mà **không harness đơn lẻ nào làm được**, vì cả bốn là
tính chất của *tập hợp*:

- **A. Chạy hết cả 7.** CI trước đó chỉ chạy `phase-01.sh`. Đó chính là lý do phase-04 ruỗng mất
  hai mutation suốt năm phase: nó exit 1 với `matched nothing` — đúng, to tiếng, và **không ai
  nghe**.
- **B. Đối chiếu manifest.** Mỗi harness khai trước số mutation must-fail và số control
  must-stay-green; script đếm outcome thật và so. Một mutation bị xoá / comment out / thôi áp được
  làm phép tính **sai** ⇒ fail build. Đây là chỗ duy nhất "ruỗng" hiện ra: harness vẫn exit 0, chỉ
  báo ít catch hơn, và trông y như một lượt chạy tốt.
- **C. Áp rỗng là hạng mục lỗi riêng.** Không gộp vào "có mutation fail", vì hai cái cần hai cách
  sửa khác nhau.
- **D. Checksum cây nguồn trước/sau mỗi harness.** Luật 5 *yêu cầu* harness tự restore; đây là chỗ
  kiểm tra lời hứa đó từ bên ngoài. Một harness restore hỏng để mutation nằm lại, và mọi đo đạc
  sau đó — **kể cả của các harness khác trong cùng lượt chạy** — vô nghĩa.

Kèm một sửa nhỏ trong `phase-07.sh` mà phase 4–6 chưa có: `trap` cho `INT`/`TERM` **restore rồi
`exit 130`**. Ở phase 4–6 handler return xong bash đi tiếp sang mutation kế, nên một `kill` để
harness chạy tiếp trên cây vừa được restore rồi `_apply` đầu độc lại. Restore là tính chất an
toàn luật 5 đòi; **dừng** là cái làm `kill` có nghĩa như người bấm nó nghĩ.

### §7.3 — tại sao bản "hiển nhiên" của test download vô giá trị

`Promise.all` là **dispatch song song, không phải thực thi chồng lấn**, và đếm số **lần gọi** CDN
client trả lời một câu hỏi khác với số request **đang bay cùng lúc**. Nên `OverlapMeter` đếm
request đang **mở đồng thời trên một HTTP server thật**, và **giữ** mỗi request lại tới khi đủ số
kỳ vọng cùng mở — nên một lượt chạy không hề chồng lấn không thể pass bằng cách nhanh, nó chờ hết
ceiling rồi báo con số thật (thấp). Lane lấy ra từ **container thật** với `DOWNLOAD_CONCURRENCY=3`
(cố ý **không** phải mặc định 6, để không lẫn "đọc env" với "hardcode 6"), và có **positive
control** dựng 5 lane đòi dụng cụ đo báo được số 5 — vì "không bao giờ quá 3" chỉ là bằng chứng
nếu cái đo *nhìn thấy được* số 4.

Hai mutation, một cho mỗi nửa của câu DoD: `{ length: 1 }` cho nửa `==`, và
`{ length: env.DOWNLOAD_CONCURRENCY + 2 }` cho nửa **"không hơn"**.

### Quyết định phát sinh

- **`ProbeHealthCheck` to tiếng, KHÔNG fail fast** — lệch có chủ ý so với Risks dòng 1. Check fatal
  làm cả dịch vụ không boot mỗi khi Douyin/parser tạm không tới được, kéo theo tạo job, browse và
  endpoint cookie — không cái nào liên quan tới probe — để ngăn một người vận hành đi sai đường.
  Hại ngăn được **nhỏ hơn** hại tạo ra. Nên nó log ở mức `error`, nói **hệ quả** chứ không chỉ
  triệu chứng ("mọi 3×400 từ giờ sẽ báo `blocked`, kể cả profile chỉ là private"), và ghi verdict
  vào `healthy` để readiness endpoint của phase 10 dùng lại được mà không phải bắn thêm request.
  `healthy` có **ba** trạng thái, không phải boolean: "chưa kiểm" không được đọc thành "không khoẻ",
  nếu không mọi node API-only sẽ mãi không ready.
- **Probe chỉ chạy ở node có lane** (`LANES_ENABLED`). Node API-only không chẩn đoán gì, nên
  request đó mua không được gì — và trên một fleet là một probe/node/lần deploy vào rate limit
  theo IP. Có mutation cho **cả hai** chiều của cái gate này.
- **`EnumerateParser = PostPageSource & ProbeSource`, bắt buộc, không optional.** Probe optional
  nghĩa là lane thiếu probe âm thầm tụt về hành vi "cause UNKNOWN" của phase 5, và mutation bỏ
  wiring ở `lanes.module.ts` sẽ để **mọi** test xanh.
- **`markFailed` cho `jobs` giữ `max_cursor`.** `failed` nằm trong `RETRYABLE_STATUSES`, nên một
  status "terminal" mà ném checkpoint đi sẽ làm chính cái retry nó cho phép trở nên đắt vô cớ.

### Chưa đạt / ngoài phạm vi

- **`docker compose up` từ máy sạch vẫn KHÔNG chạy được** (`plan.md` §9). Không có gì áp migration:
  Postgres của compose khởi động **rỗng**, nên mọi route đụng DB trả 500 (`/api/jobs` 500 cho tới khi
  áp `migrations/260824-init-schema.sql` bằng tay). Test suite không thấy vì mỗi suite tự tạo schema
  riêng. Đây là việc của phase 10 (ops) — ghi ra vì nó là một dòng DoD của cả plan đang **chưa đạt**,
  không phải một chi tiết deploy.
- **Allow-list scheme/host cho `play_addr`** — để phase 10, đúng như brief.
- **"Push cookie mới → parser dùng ngay, KHÔNG restart" của §7.2 được phủ ở hai tầng khác nhau,
  không phải một.** Trong `recovery-loop.spec.ts` nó là *cùng một object parser* bị flip cờ — hình
  dạng in-process của hợp đồng, và đủ để chứng minh lane/`/retry` không cần dựng lại. Nửa
  **qua dây** (NestJS ghi Redis → parser fork đọc ở request kế) là SPIKE 2, `test/spike-cookie-swap`,
  và nó **không** chạy mỗi PR (build image fork qua mirror CN). Nói ra vì luật 9: một double không
  thất bại được theo cách thứ thật thất bại thì không được núp trong con số.
- **`scripts/run-mutations.sh` chạy hết mất ~25 phút** trên máy này. Trong CI nó là một gate bắt
  buộc và sẽ chậm. Chấp nhận có chủ ý: một gate chậm vẫn hơn một gate không chạy — đó chính là cách
  hai mutation của phase 4 chết mà không ai biết. Nếu cần chia nhỏ, script đã nhận tham số phase
  (`scripts/run-mutations.sh 04 07`).
