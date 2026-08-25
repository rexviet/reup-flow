---
name: douyin-downloader
title: "Douyin Profile Bulk Downloader — services/downloader"
status: in_progress
created: 2026-08-24
source: plans/reports/260824-2139-solution-design-douyin-downloader.md
sourceRev: 1
brainstorm: plans/reports/260824-2126-douyin-downloader-brainstorm.md
blockedBy: []
blocks: []
phases: 11
---

# Implementation Plan — Douyin Profile Bulk Downloader

**Nguồn:** solution design `260824-2139` (approved) · brainstorm `260824-2126` (approved) · `IDEA.md`
**Stack đã chốt:** pnpm monorepo `services/` · TypeScript + NestJS (API + lane **chung 1 process**) · Postgres + Drizzle · Redis (chỉ làm kênh cookie) · đĩa local sau interface `Storage` · **DB làm queue**, không broker · polling 1s · `X-Api-Key` · React + Vite + Tailwind · Chrome MV3

---

## 0. Bốn nguyên tắc bất khả xâm phạm

Mọi phase phải tôn trọng. Nếu một phase làm bạn muốn vi phạm, **dừng và sửa solution design trước**, đừng lặng lẽ đi đường khác.

1. **HTTP layer không bao giờ gọi lane.** Controller chỉ ghi row; lane chỉ đọc row. Postgres là kênh duy nhất giữa hai bên. Đây là thứ làm việc tách lane ra process riêng sau này tốn **0 dòng logic**. Enforce hai tầng: ESLint `no-restricted-imports` (bắt lúc gõ) + một test kiến trúc (bắt trường hợp lách bằng dynamic import).
2. **Không patch thêm parser fork.** `src/Douyin_TikTok_Download_API` đóng băng trong phạm vi plan này. Mọi thứ thiếu ở parser (nhất là F4 — lỗi bị bóp thành 400 vô nghĩa) **xử lý ở phía NestJS**. Lý do: bảo vệ ngân sách `scripts/check-patch-size.sh`, thứ tồn tại để còn merge được upstream khi Douyin đổi `a_bogus`.
3. **Cookie không bao giờ vào log, error message, hay API response.** Redact ở **tầng logger**, không ở từng call site. Test phải phủ **cả** logging subsystem **và** stdout/stderr — bài học phase 1: test cũ chỉ nhìn logging module nên `print()` lọt qua.
4. **Guard nào cũng phải có mutation test.** Phá invariant → **bắt buộc đỏ**. Và phải verify mutation **thật sự match**: phase 1 có hai lần `sed` mutation match rỗng, nên "suite vẫn xanh" là mutation tồi chứ không phải gap thật. Xem §5.

---

## 1. Trạng thái xuất phát

| Đã có | |
|---|---|
| Parser fork submodule `@5fe4e4d` | cookie đọc từ Redis `douyin:cookie:current` **mỗi request**, async, client cache theo event loop, timeout 1s, fallback env → `config.yaml` |
| `update_cookie()` upstream | đã xoá hẳn (nó ghi cookie vào file tracked + print stdout 3×) |
| CI gate | gitleaks + rule `douyin-session-cookie` + canary · `check-patch-size.sh` · ~20 pytest trong fork |

| Chưa có | orchestrator · lane · DB · frontend · extension · compose |

**Route parser đã verify bằng code** (prefix `/api/douyin/web/`, envelope `{code, router, data}`):
`fetch_user_post_videos(sec_user_id, max_cursor, count)` · `fetch_one_video(aweme_id)` · `get_sec_user_id(url)` (xử lý luôn short link) · `handler_user_profile(sec_user_id)`

---

## 2. Bảy quyết định/phát hiện phải nằm đúng chỗ

F1–F3 duyệt ở brainstorm; F4–F7 phát hiện ở solution design. **Ba trong số đó là bug nếu để cuối.**

| ID | Nội dung | Phase | Tại sao đúng chỗ đó |
|---|---|---|---|
| F1 | Cache `play_addr` + `url_expires_at`, re-resolve **phản ứng** khi hết hạn/403/410 | 1 (cột) + 6 (logic) | Làm đúng IDEA.md §3.2 tốn **525 req** cho profile 500 video thay vì 25 ⇒ ban gần như chắc |
| F2 | Enumerate concurrency = **1 toàn hệ thống** | 5 | Rate limit theo **IP**, không theo job. 3 job song song = 3× tần suất = ban |
| F3 | `videos.lease_expires_at` + reaper | 1 (cột) + 4 (reaper) | Không có thì video kẹt `downloading` vĩnh viễn |
| **F4** | Probe disambiguation cho lỗi parser | 7 | Parser bóp **mọi** lỗi thành 400 `"An error occurred."`. Làm đúng IDEA.md §6.2 sẽ báo `blocked` cho profile private ⇒ người vận hành đi đổi cookie vô ích |
| **F5** | `jobs.lease_expires_at` | **1** | Crash giữa enumerate để job đứng vĩnh viễn ở `enumerating`; lane chỉ nhặt `pending` ⇒ job chết hẳn. **Thêm cột lúc chưa có dữ liệu là miễn phí, thêm sau là migration** |
| **F6** | `attempts` tăng **lúc claim** | **1** | Tăng lúc fail thì crash-loop không bao giờ tăng ⇒ claim lại vô hạn |
| **F7** | Tiến độ đo theo **trạng thái profile**, không theo delta job | 1 (ghi thành doc) | `videos` khoá `aweme_id` toàn cục — chính là cái làm incremental miễn phí. Job lần 2 hiện `340/340` ngay: **đúng, không phải bug**. Ghi rõ để không ai "sửa" nó |

---

## 3. Một gate cứng

```
Phase 0 ──GATE──> Phase 1..10
```

**Gate sau Phase 0.** Hai câu phải trả lời bằng **bằng chứng chạy được**, không phải đọc doc:

1. Drizzle diễn đạt được `FOR UPDATE SKIP LOCKED` **không cần rơi xuống raw SQL**? → chứng minh bằng integration test 2 transaction song song, khẳng định transaction thứ hai **bỏ qua** row đã bị khoá thay vì chờ. Không đạt ⇒ **đổi ORM ngay tại đây**, trước khi có bất kỳ dòng domain code nào. (Prisma đã bị loại từ solution design: cần `$queryRaw`, mất type safety đúng chỗ quan trọng nhất.)
2. NestJS ghi Redis → parser đọc được cookie mới ở **request kế tiếp, không restart**? → đây là hợp đồng đã implement ở phase 1 của fork nhưng **chưa bao giờ được verify từ phía Node**.

Lý do đặt thành gate: cả hai đều là *giả định về thư viện/hợp đồng bên ngoài*. Sai câu 1 thì D5 (DB làm queue) sụp và phải chọn lại ORM. Sai câu 2 thì toàn bộ phần tự động hoá cookie sụp.

**GATE CLEARED** (`0b09d8c`). Câu 1: PASS, Drizzle giữ nguyên, không đổi TypeORM. Câu 2: PASS, verify bằng parser fork gốc không sửa. Chi tiết + 5 false-green bắt được: `phase-00-skeleton-spike.md` §Outcome.

---

## 4. Phase index

| # | File | Nội dung | Effort | Blocked by | Milestone |
|---|---|---|---|---|---|
| 0 | `phase-00-skeleton-spike.md` | pnpm workspace · compose 4 container · **2 spike gate** — **DONE** (`0b09d8c`), cả 2 gate PASS + mutation | ~0.5d | — | **GATE ✅** |
| 1 | `phase-01-schema.md` | Drizzle schema + migration + claim/reaper query. **F1·F3·F5·F6 cột, F7 doc** — **DONE**, 8 mutation đỏ + test chống drift schema↔migration | ~0.5d | 0 | M1 ✅ |
| 2 | `phase-02-ports.md` | `ParserClient` (bóc envelope) + `Storage` (`.tmp`→rename, `freeBytes`) — **DONE**, 15 mutation đỏ + 1 control xanh, integration với parser thật | ~0.5d | 1 | M1 ✅ |
| 3 | `phase-03-http-api.md` | jobs/videos controller · cookie endpoint · `X-Api-Key` · redaction · **ESLint boundary** — **DONE**, 331 test + 18 mutation đỏ + 2 control xanh; review đối kháng bắt 3 escape redaction + 2 test không thể đỏ | ~1d | 2 | M1 ✅ |
| 4 | `phase-04-lane-framework.md` | `LaneRunner` base (bắt hết exception) · graceful shutdown · reaper (F3+F5+finalize+dọn `.tmp`) — **DONE**, 20 mutation đỏ (19 test + 1 ESLint) + 2 control xanh; bắt được 4 bug thật, nặng nhất là `onModuleDestroy` đóng pool trước khi lane nhả lease | ~0.5d | 2 | M1 ✅ |
| 5 | `phase-05-enumerate-lane.md` | global 1 luồng (F2) · checkpoint mỗi trang · blocked theo tín hiệu cấu trúc — **DONE**, 32 mutation đỏ + 2 control xanh; tìm ra bug phase 4 đã ship (`greatest()` trên `max_cursor` sai hướng vì cursor Douyin **giảm** dần), và review đối kháng chứng minh **4 assertion không thể đỏ** + 1 guard pacing không có test nào | ~0.5d | 4 | M1 ✅ |
| 6 | `phase-06-download-lane.md` | claim SKIP LOCKED · F1 re-resolve phản ứng · stream → rename · finalize job | ~1d | 5 | **M1 gate** |
| 7 | `phase-07-durability.md` | **F4 probe** · retry/cancel · rate-limit test · **bộ mutation test** | ~1.5d | 6 | **M2** |
| 8 | `phase-08-web-ui.md` | React+Vite+Tailwind · hai thanh riêng · badge cookie · polling 1s | ~1d | 3 | M2 |
| 9 | `phase-09-extension.md` | MV3 `chrome.cookies` + `alarms` 20′ + options page | ~0.5d | 3 | **M3** |
| 10 | `phase-10-ops.md` | preflight đĩa · CI pull upstream hàng tuần · browse polish | ~0.5d | 7, 8 | **M4** |

**Tổng ~9 ngày code.** Không tính review người, không tính thời gian chờ job thật chạy.

Phase 8 và 9 chỉ cần Phase 3 (API), nên **làm song song được** với 4–7 nếu có người thứ hai.

---

## 5. Bảng mutation test bắt buộc

Đây là §13 brainstorm nâng thành yêu cầu thi hành. **"Suite xanh" không phải bằng chứng invariant được giữ** — nó chỉ là bằng chứng suite đã chạy.

| Invariant | Phase | Mutation | Kỳ vọng |
|---|---|---|---|
| Loop rò exception không giết lane | 4 | Ném lỗi trong thân loop | Lane vẫn sống, test xanh; **bỏ `try/catch` → test đỏ** — **DONE**, 6 test đỏ |
| Reaper nhặt lại video mồ côi | 4 | Bỏ reaper `videos` | Test resume **đỏ** — **DONE**, kèm **control**: bỏ sweep này thì test SIGTERM-nhả-lease phải **vẫn xanh**, nếu không thì nó đang đo reaper chứ không đo "ngay lập tức" |
| Reaper nhặt lại **job** mồ côi (F5) | 4 | Bỏ reaper `jobs` | Test resume enumerate **đỏ** — **DONE** |
| `attempts` tăng lúc claim (F6) | 1 | Chuyển sang tăng lúc fail | Test crash-loop 3 lần **đỏ** — **DONE**, 7 test đỏ |
| Claim không trùng | 1 | Bỏ `SKIP LOCKED` / bỏ `FOR UPDATE` | Test 2 transaction song song **đỏ**. Phase 0 đã proof pattern này (2 mutation, cả 4 test đỏ) — **lưu ý cho phase 1**: barrier `Promise.all` không đủ, `LockRows` của Postgres tự thử row kế tiếp nên tự "chữa lành" thiếu `SKIP LOCKED` khi commit nhanh; claim thắng phải giữ transaction mở tới khi mọi contender kết thúc. Shape đã validate: `.for('update', { skipLocked: true })` + `inArray(column, subquerySelect)`. **DONE ở phase 1** trên bảng `videos` và `jobs` thật, dùng lại nguyên harness của phase 0 |
| Enumerate global 1 luồng (F2) | 5 | Đổi thành per-job | Test tần suất 2 job **đỏ** — **DONE**. Đo `maxInFlight` của parser qua **mảng `LANES` production** (F2 là thuộc tính của mảng, không phải của class), kèm **positive control** phải báo 2 khi thật sự có 2 lane: "max in-flight = 1" chỉ là bằng chứng nếu dụng cụ đo *nhìn thấy được* số 2. Kèm **control phải-xanh**: thêm reaper thứ hai ⇒ suite tần suất enumerate **vẫn xanh**, chứng minh nó đo request chứ không đo độ dài mảng |
| `blocked` ≠ `failed` | 5, 7 | Map `blocked` → `failed` | Test hồi phục cookie **đỏ** — **DONE ở phase 5**, nhưng chỉ sau khi bỏ phiên bản hiển nhiên của nó: `RETRYABLE_STATUSES` chứa **cả hai**, nên test chỉ đi hết luồng recovery **vẫn xanh** khi map sang `failed`. Neo vào (a) chính cái từ — cả hai retryable nên status **là** chỉ dẫn remediation — và (b) `CANCELLABLE_STATUSES` chứa `blocked` mà loại `failed`, là hệ quả *hành vi* của cái từ. Control phải-xanh: đổi lời log ⇒ vẫn xanh |
| Ghi file atomic | 6 | Bỏ `.tmp` + `rename` | Test crash-giữa-ghi **đỏ** — **DONE ở phase 2** (`Storage` sinh ra ở đó, nên guard sinh ra cùng chỗ): 2 mutation (`.tmp`+rename, tên `.tmp` cố định) đều đỏ, crash test dùng `SIGKILL` thật ở tiến trình con. **Hai bài học**: (a) mutation "tên cố định" ban đầu đỏ vì cờ `wx` chặn writer thứ hai chứ không vì corruption — chữa bằng **control phải-xanh**: gỡ *chỉ* `wx`, giữ tên duy nhất ⇒ suite vẫn xanh. (b) **Kể cả sau đó nó vẫn đỏ vì lý do sai**: writer thua `rename` trên temp file đã bị dời đi, ăn `ENOENT`, chết **trước** assertion về nội dung. Thêm đúng thứ phase 6 sẽ thêm — `rename` khoan dung `ENOENT` — thì test **flaky-green 3/6**. Gốc rễ: hai payload cùng độ dài, ghi cùng offset, nên file dùng chung vẫn có thể trông đồng nhất. Sửa: hai writer **độ dài khác nhau** + đòi file bằng **byte-for-byte** một trong hai payload, và thêm mutation mô phỏng sẵn hình dạng phase 6 |
| Cookie không vào log | 3 | Thêm `log(cookie)` **và riêng biệt** `console.log(cookie)` | **Cả hai** phải **đỏ** — **DONE**, 2 mutation bắt được |
| Checkpoint mỗi trang | 5 | Chuyển checkpoint ra sau vòng lặp | Test resume giữa enumerate **đỏ** — **DONE**. Quan sát từ **bên trong** lời gọi parser (trang N+1 đọc row ra), vì "đã ghi rồi" chỉ có nghĩa ở đúng điểm đó |
| `max_cursor` tiến theo **tiến độ**, không theo giá trị số | 5 | Đặt `greatest()` trở lại | Test cursor **giảm dần** **đỏ** — **DONE**, đo ở cả tầng repository và **qua lane**. Đây là bug phase 4 đã ship: cursor Douyin là `create_time` của item **cũ nhất** trong trang và profile phân trang **mới nhất trước**, nên cursor **giảm**; `greatest` đóng băng checkpoint ở trang 1 trong khi `enumerated_count` vẫn tăng và lease vẫn renew — **không quan sát nào thấy sai**. Không test nào bắt được vì fake parser tự do trả số **tăng**: fixture dễ hơn thực tế là một dạng false-green riêng |

**Luật thi hành:** mỗi mutation phải in ra **diff nó thật sự áp được**. Mutation match rỗng ⇒ coi như **chưa chạy**, không phải "đã pass". Đây là cái bẫy đã sập hai lần ở phase 1.

**Luật thứ hai (thêm ở phase 2):** khi một invariant có thể do **nhiều hơn một cơ chế** giữ, phải kèm một **control phải-xanh** gỡ cơ chế yếu hơn. Mutation đỏ chỉ nói dòng bị đụng *có vai trò gì đó*; nó không nói vai trò nào. Xem `test/mutations/phase-02.sh` `run_control`.

**Luật thứ ba (thêm ở phase 3): mutation chỉ là bằng chứng khi code **thật sự đổi** VÀ test **thật sự chạy**.** Luật thứ nhất chỉ phủ nửa đầu. Nửa sau sập ngay ở phase 3: `pnpm test -- "$pattern"` với pattern nhiều từ truyền cả chuỗi thành **một** regex đường dẫn, khớp 0 test, exit 1. Chỉ nhìn exit code thì không phân biệt được với "mutation bị bắt" — nên 5 mutation must-fail báo "caught" mà chưa chạy gì, và 2 control must-stay-green báo "đỏ" trong khi code hoàn toàn đúng. **Sai cả hai chiều, và cả hai chiều đều trông hợp lý.** Sửa: word-split pattern, và `No tests found` là **lỗi cứng**, không phải tín hiệu theo chiều nào.

**Luật thứ tư (thêm ở phase 3): một cơ chế restore duy nhất, áp cho *mọi* file, và phải verify.** Mutation đụng hai file thì (a) đòi **mọi** file được liệt kê phải đổi — "ít nhất một file đổi" cho phép mutation áp một nửa rồi vẫn tự báo "caught"; và (b) không dùng `git checkout --` để trả file về: với file git **chưa track** — ở một phase mới thì đó là *tất cả* — nó im lặng không làm gì, mutation sống tiếp hết lượt chạy, và mọi khẳng định sau đó đo trên một build đã hỏng.

**Luật thứ năm (thêm ở phase 4): harness bị *kill* phải không để lại cây nguồn nhiễm độc.** Luật 1–4 đều giả định script chạy tới được lệnh `restore` của chính nó. Một watchdog `pkill` giữa `_apply` và `restore` để lại mutation nằm nguyên trong file **chưa track** — mà ở một phase mới thì đó là *mọi* file, và `git checkout` không có gì để trả về. Lượt chạy kế tiếp đo trên build đã hỏng: mutation thứ hai báo `matched nothing`, mọi kết quả sau đó vô nghĩa. Sửa: snapshot **pristine** mọi file mục tiêu trước khi chạy, `trap ... EXIT INT TERM` restore, và **in ra** là đã phải restore (restore im lặng không phân biệt được với không restore).

**Luật thứ sáu (thêm ở phase 4): một mutation có thể *treo* thay vì fail.** Mutation "bỏ `setImmediate` sau tick `'worked'`" không làm test đỏ — nó làm event loop ngừng quay, nên **timeout của chính jest cũng không nổ** và suite đứng vĩnh viễn tại đó. Sửa ở hai đầu: (a) harness có timeout riêng và coi hang là **một outcome riêng** — hang là catch hợp lệ cho must-fail, là *thất bại* cho control, không bao giờ gộp vào "đỏ" chung (nếu gộp thì "Postgres sập" sẽ trông như 16 mutation thành công); (b) test tự sửa để **đỏ được**: cap số tick của lane giả + probe bằng `setImmediate` chứ không `setTimeout`, vì event loop bị đói cũng làm đói luôn cái đo nó. **Một mutation không bắt được là một guard không được canh.**

**Luật thứ bảy (thêm ở phase 5): fixture dễ hơn thực tế là một dạng false-green riêng, và mutation "vẫn xanh" là kênh duy nhất bắt được comment đang nói dối.**

Hai chuyện ở phase 5, cùng một gốc: *test chỉ chứng minh được điều fixture cho phép nó chứng minh.*

(a) `checkpoint` của phase 4 dùng `greatest()` trên `max_cursor`. Cursor Douyin **giảm** dần, nên guard đó đóng băng checkpoint ở trang 1 — nhưng mọi fake parser trong repo trả cursor **tăng**, nên **không mutation nào, không test nào** bắt được. Sửa: fixture phải mô phỏng *hướng* thật, và mutation đặt `greatest()` trở lại phải đo **qua lane** chứ không chỉ ở repository.

(b) `upsertMany` có `Map` dedupe kèm comment nói `ON CONFLICT` sẽ raise "cannot affect row a second time". Mutation phá `Map` **vẫn xanh** ⇒ comment sai. Đo thật: `DO NOTHING` trả 1 row không lỗi, chỉ `DO UPDATE` mới raise. `Map` là dead code. Nếu "mutation stayed green" bị coi là chuyện nhỏ thì cái comment sai đó sống mãi và trở thành lý do người sau *không* dám đổi clause.

**Hệ quả đáng nhớ:** harness là thứ chấm điểm mọi guard khác, nên bug trong nó vô hình theo đúng nghĩa đen — nó hỏng bằng cách báo cáo sự tự tin. Bảy lần rồi. Trước khi tin một lượt chạy, kiểm từng mutation có đổi đúng file nó khai, có chạy **khác 0** test, có **kết thúc**, và — với guard nào dựa vào hình dạng dữ liệu bên ngoài — fixture có mô phỏng đúng *hình dạng đó* hay chỉ mô phỏng một phiên bản dễ hơn.

---

## 6. Risk register — chỉ những cái đổi hành vi lúc code

| Risk | Phase | Xử lý trong code |
|---|---|---|
| ~~Drizzle không diễn đạt được `SKIP LOCKED`~~ | 0 | **RESOLVED — gate PASS (`0b09d8c`).** Drizzle giữ nguyên |
| ~~Migration `claimOne` seq-scan dưới tải~~ | 1 | **RESOLVED.** Partial index đã tạo; `claim-plan.spec.ts` EXPLAIN trên 50k row khẳng định planner dùng đúng index, và mỗi khẳng định đã được chứng minh đỏ được bằng cách xoá index |
| Fault-injection sót lại trong `main.ts` | 1 → 2 → **3** | `services/downloader/src/main.ts` có nhánh test-only `__TEST_ONLY_SIMULATE_BOOTSTRAP_ERROR__` (tắt khi `NODE_ENV=production`). Xoá khi DB/Redis client thật cho bootstrap catch-all lỗi thật để test. **Vẫn còn sau phase 2**: phase 2 chỉ thêm hai port do *lane* dựng (`ParserClient`, `Storage`), `NestFactory` vẫn không mở kết nối nào; và test D-3 cần một lỗi **có nhúng DSN**, thứ chỉ lỗi kết nối DB/Redis sinh ra — dời sang phase 3 |
| `redactSecrets` ở `src/config/env.ts` là tạm | 3 | **RESOLVED.** Tầng logger phase 3 thay thế (`redact.logger.ts` + stdio patch). Stopgap xoá |
| `start.py` fork chạy `uvicorn --reload` cả ở production | 10 | Chưa quyết — xem §8 câu hỏi mở |
| ~~Loop rò exception → lane chết âm thầm~~ | 4 | **RESOLVED.** `try/catch` toàn thân + `lastTickAt` + watchdog 5 phút ở `LaneSupervisor` (một `setInterval` unref'd, **không** phải một lane — lane không tự canh được cái chết của chính nó). Mutation đỏ. **Bug kèm theo, cùng họ:** một tick `'worked'` chỉ `await` microtask sẽ làm đói event loop, và vì API + lane chung process thì đó là `/health` im lặng + SIGTERM handler không chạy — sửa bằng `setImmediate` mỗi tick, có mutation |
| ~~`.tmp` mồ côi không ai dọn~~ | **4** | **RESOLVED.** `Storage.removeStaleTemp(olderThanMs)` + sweep thứ tư của reaper, cutoff = một `LEASE_SECONDS` đo bằng **mtime** (file đang được ghi thì mtime còn chạy) cộng registry in-flight trong process, nên sweep cutoff-0 vẫn không đụng download đang sống. Xoá `.tmp` của writer còn sống làm `rename` của nó ăn ENOENT — đúng lỗi journal phase 2 mất một ngày. Lý do cũ: | Một worker bị `SIGKILL` giữa chừng để lại `{key}.{uuid}.tmp` **vĩnh viễn** — `local-disk.storage.spec.ts` khẳng định đó là hành vi đúng (không được commit gì), nhưng **không có code nào xoá nó**. Reaper phase 4 hồi phục *row DB*, không hồi phục *bytes*. Qua một vòng crash-loop chúng tích lại ở kích thước video đầy đủ, `exists()` không thấy, mà vẫn ăn đúng phần đĩa `freeBytes()` hứa với preflight phase 10. Reaper phase 4 phải dọn luôn `.tmp` quá hạn của key nó nhả |
| Ai đó gọi `setGlobalDispatcher(new RetryAgent(...))` ở file khác | ~~2~~ | **RESOLVED ở phase 2.** `undici.request` dùng dispatcher **toàn cục** nếu không được đưa dispatcher riêng, nên một thư viện bất kỳ trong process biến 1 lời gọi parser thành 4 — **không đụng một dòng nào của `parser.client.ts`**, tức là không mutation nào của phase 2 bắt được. `ParserClient` giờ sở hữu `Agent` riêng và có `close()` cho graceful shutdown phase 4. Có test + mutation |
| Reaper nhả video mà worker vẫn đang tải → 2 writer 1 file | 6 | `.tmp` mang tên **duy nhất theo lượt claim**; `rename` là điểm commit duy nhất; **kiểm lease trước khi `rename`**, hết lease thì tự huỷ |
| Chẩn đoán `blocked` sai (F4) | 7 | Ưu tiên tín hiệu cấu trúc (`aweme_list` rỗng + `has_more=1`, là HTTP 200 và **chắc chắn**); probe chỉ dùng sau 3× 400 |
| Cookie lọt vào log | 3 | **RESOLVED.** Redaction layer (`redactor.ts`, `stdio-redaction.ts`, `redact.logger.ts`) phủ cả logging + stdout/stderr. 2 mutation (logger.log / console.log riêng) đều bắt được |
| Cookie bị ban giữa job | 5, 7 | **RESOLVED ở phase 5** cho tín hiệu cấu trúc: `blocked` + giữ `max_cursor`, không phải `failed`. 3× 400 cũng park ở `blocked` nhưng **nói rõ nguyên nhân là UNKNOWN** kèm `TODO(phase-7)` — probe F4 là phase 7 |
| Douyin đổi `a_bogus` | 10 | CI job hàng tuần: fetch upstream → `check-patch-size.sh` → pytest fork → grep `PATCH_NOTES.md` §5 → mở issue nếu lệch |
| Upstream hoist header thành constant lúc import | 10 | Cookie **đóng băng lúc process start, không có exception**. Watch tỉ lệ job `blocked`, không watch exception |
| Đầy đĩa giữa job | 10 | Preflight `total_videos × 30MB > freeBytes − 10GB` → từ chối. Chạy **sau** enumerate |
| IP Việt Nam bị chặn | vận hành | Proxy trong `config.yaml` của fork. Cần throughput hơn: nhiều instance parser sau proxy khác nhau |

---

## 7. Lệch có chủ ý so với IDEA.md

Ghi ra để không ai "sửa lại cho đúng spec" rồi tạo bug.

| IDEA.md nói | Plan làm | Lý do |
|---|---|---|
| §3.2 không cache `play_addr`, re-resolve trước mỗi lần tải | Cache + re-resolve **phản ứng** (F1) | 525 req vs 25 req cho profile 500 video. Enumerate đã trả `play_addr` sẵn |
| §6.2 "3 lỗi liên tiếp → `blocked`" | Tín hiệu cấu trúc trước; 3× 400 thì **probe** rồi mới quyết (F4) | Parser không phân biệt được 4 nguyên nhân. Sai chẩn đoán = remediation sai |
| §5.2 có `GET /api/jobs/{id}/events` SSE | **Không có SSE ở v1**, polling 1s | Job hàng giờ, trễ 1s vô nghĩa; reconnect miễn phí |
| §9 M1 "cookie paste tay qua ô text tạm trên UI" | `POST /internal/cookie` bằng curl, **không xây ô text throwaway** | Cùng một bằng chứng, không tốn công cho UI sẽ bị xoá ở M3 |
| §11 "đặt lịch kiểm upstream hàng tuần" | **CI job**, không nhắc người | `PATCH_NOTES.md` tự ghi rằng nó chỉ khuyến nghị và không được CI enforce. Con người sẽ không nhớ |

---

## 8. Câu hỏi mở

**Đã trả lời ở solution design:** ngưỡng đĩa (`total_videos × 30MB` vs `freeBytes − 10GB`, chạy sau enumerate) · lịch pull upstream (CI hàng tuần).

**Hoãn có chủ ý, không chặn code:**
1. `DATA_DIR` mount ở đâu, quota bao nhiêu → lúc deploy. `Storage.freeBytes()` đọc lúc chạy, thiết kế không giả định con số.
2. Preview video trong UI → quyết ở Phase 10, **mặc định KHÔNG**. v1 chỉ trả `storage_path`. Thêm serve static sau = một route Nest, additive.

**Đã trả lời ở phase 0:** API chính xác của `FOR UPDATE SKIP LOCKED` trong Drizzle → gate PASS bằng test, xem phase-00 §Outcome.

**Còn mở:**
3. Fork `start.py` hardcode `uvicorn.run(..., reload=True)` ⇒ parser chạy file-watching reloader + spawned worker kể cả ở production. Hướng khả dĩ: root compose override `command:` mà không đụng file fork. **Ghi là quyết định mở, chưa làm** — quyết ở phase 10.
4. Gate cookie-swap SPIKE 2 trong CI chạy weekly / `workflow_dispatch` / nhãn PR `run-cookie-gate` — không chạy mỗi PR vì phải build image fork qua CN PyPI mirror. Cần ghi nhận một lần chạy tay trước khi bắt đầu phase 1.

---

## 9. Definition of done cho cả plan

- [ ] Đổi cookie trong Redis → parser dùng cookie mới ở request kế tiếp, **không restart**
- [ ] Job profile 200+ video hoàn tất, số file trong storage khớp `downloaded_count`
- [ ] Kill process giữa pha download → restart → tiếp tục, **không tải lại file đã xong**
- [x] Kill process giữa pha **enumerate** → restart → tiếp từ `max_cursor` (F5) — **DONE ở phase 5**
- [ ] Chạy lại cùng profile lần 2 → **chỉ tải video mới**
- [ ] Cookie hết hạn giữa job → `blocked` (không `failed`) → push cookie mới → `/retry` → tiếp từ checkpoint
- [ ] Profile private → `failed` kèm lý do rõ, **không phải** `blocked` (F4)
- [ ] Cookie không xuất hiện trong bất kỳ log / API response nào
- [ ] `docker compose up` từ máy sạch chạy được
- [ ] Parser **không** expose ra ngoài internal network (không có `ports:`)
- [x] Enumerate 2 job song song → tổng tần suất request vẫn = 1 luồng (F2) — **DONE ở phase 5**, đo qua mảng `LANES` production + positive control
- [x] Test kiến trúc xanh: nạp tay một row `jobs` ở `pending` thì lane vẫn chạy (chứng minh HTTP layer và lane chỉ nối qua DB) — **DONE ở phase 4**
- [ ] **Toàn bộ 10 mutation ở §5 đã chạy, mỗi cái in diff áp được, mỗi cái đỏ**
