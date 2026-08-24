---
phase: 1
title: Repo + patch cookie cho parser fork (M0)
status: completed
effort: 0.5 ngày
blockedBy: [0]
---

# Phase 1 — Repo + parser fork

Milestone **M0** của solution design §13. Nhỏ nhưng phải xong trước mọi thứ khác, vì Module 1 không chạy được nếu cookie không đổi được lúc runtime.

---

## 1.1 Scrub cookie trong config — *đã điều tra, hạ mức độ*

**Kết quả kiểm 2026-08-24 (S3 bước 1):**

| Câu | Trả lời |
|---|---|
| File nào chứa cookie thật? | **`crawlers/douyin/web/config.yaml`** — không phải `config.yaml` ở gốc như solution design §7.2 viết |
| Fork có public không? | **Có** — `api.github.com/repos/rexviet/Douyin_TikTok_Download_API` trả 200 không cần auth |
| Cookie đó của ai? | **Của upstream.** `git log` → commit `3b491db`, author `Evil0ctal`, 2025-03-16. Fork chỉ thừa hưởng, không thêm gì |

⇒ **Không có cookie của ta bị lộ. Không cần thu hồi gì.** Risk 🔴 ở solution design §7.2 phát hiện 4 đúng về sự kiện nhưng sai về chủ thể — đó là cookie của tác giả upstream, public trong repo gốc từ 2024, gần như chắc chắn đã chết từ lâu.

**Nhưng vẫn scrub, và đây là phần có giá trị thật của mitigation:** khi file tracked không còn chỗ để đặt cookie thì cookie **của ta** không bao giờ đậu vào đó được. Đó là phòng lỗi tương lai, không phải dọn lỗi quá khứ.

1. Scrub trong `crawlers/douyin/web/config.yaml`:
   ```diff
   -      Cookie: __ac_nonce=067d687ac00d70af16eab; __ac_signature=...
   +      Cookie: ""
   ```
2. Cookie fallback từ nay **chỉ đi qua env `DOUYIN_COOKIE_FALLBACK`** — đóng luôn đường commit nhầm.
3. Hai file `crawlers/tiktok/{app,web}/config.yaml` cũng có cookie upstream (`CykaBlyat=XD`, `tt_csrf_token=...`). **Không dùng, không cần sửa** — ghi ở đây để lần sau grep thấy thì biết đã xét rồi.

---

## 1.2 Git init + submodule

Repo ngoài **chưa `git init`**. Fork thì đã có git riêng với remote upstream.

```bash
cd /Users/vietphung/Data/reup-flow
git init
printf 'node_modules/\ndist/\n.env\n/data/\n' > .gitignore
git rm -r --cached src/Douyin_TikTok_Download_API 2>/dev/null || true
git submodule add https://github.com/rexviet/Douyin_TikTok_Download_API.git src/Douyin_TikTok_Download_API
```

**Trong fork, thêm remote `upstream` — hiện chưa có.** `git remote -v` chỉ có `origin`, nên check `git diff upstream/main ≤ 50 dòng` ở §1.5 **không chạy được** cho tới khi:

```bash
cd src/Douyin_TikTok_Download_API
git remote add upstream https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git
git fetch upstream
```

Fork hiện **không có commit nào lệch upstream** (HEAD = `42784ff`, author `Evil0ctal`) ⇒ baseline của patch là sạch, mọi dòng trong `git diff` sắp tới đều là của ta.

Fork **phải là submodule**, không phải thư mục copy — nếu không thì `PATCH_NOTES.md` vô nghĩa và `git diff upstream/main` không chạy được.

---

## 1.3 Patch cookie — 1 file mới + 2 dòng sửa + 3 dòng xoá + 1 dòng scrub

Khảo sát ở §7.2 đã xác định chính xác điểm patch. **Không cần đổi luồng gọi** — `get_douyin_headers()` đã đọc `config` dict mỗi lần gọi, và nó được gọi ở đầu mọi method fetch.

### File mới: `crawlers/douyin/web/cookie_provider.py` (~28 dòng)

```python
import logging, os
logger = logging.getLogger(__name__)
_KEY = "douyin:cookie:current"
_client = None
_warned = False

def _redis():
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL")
        if not url:
            return None
        import redis
        _client = redis.Redis.from_url(url, decode_responses=True,
                                       socket_timeout=1, socket_connect_timeout=1)
    return _client

def get_cookie():
    # Trả cookie từ Redis, hoặc None để caller dùng fallback.
    # KHÔNG BAO GIỜ log giá trị cookie, và không log cả str(e) —
    # message của redis-py có thể chứa nội dung reply.
    global _warned
    try:
        r = _redis()
        return r.get(_KEY) if r else None
    except Exception as e:
        if not _warned:
            logger.warning("cookie_provider: Redis unavailable (%s), using fallback",
                           type(e).__name__)          # CHỈ tên class, không phải str(e)
            _warned = True
        return None

def fallback_cookie():
    return os.environ.get("DOUYIN_COOKIE_FALLBACK", "")
```

`socket_timeout=1` là bắt buộc — Redis treo mà không timeout sẽ treo cả request Douyin.

### Sửa `crawlers/douyin/web/web_crawler.py`

```diff
+from crawlers.douyin.web.cookie_provider import get_cookie, fallback_cookie

 async def get_douyin_headers(self):
-                "Cookie": douyin_config["headers"]["Cookie"],
+                "Cookie": get_cookie() or fallback_cookie() or douyin_config["headers"]["Cookie"],

 async def update_cookie(self, cookie: str):
-        print('DouyinWebCrawler before update', config[...]["headers"]["Cookie"])
-        print('DouyinWebCrawler to update', cookie)
-        print('DouyinWebCrawler cookie updated', config[...]["headers"]["Cookie"])
```

Ba `print()` ở dòng 361, 362, 365 **in cookie ra stdout**. Xoá, không phải comment out.

### `utils.py` KHÔNG patch

`TokenManager` chỉ lấy `msToken`, `ttwid`, `proxies`; `gen_ttwid()` tự POST lên Douyin lấy ttwid mới, **không đọc `headers.Cookie`**. Đây là lý do diff xuống ~30 dòng.

⚠️ Nhưng `web_crawler.py` và `utils.py` **mỗi file `yaml.safe_load` riêng** ⇒ hai bản copy config độc lập trong bộ nhớ. Hôm nay vô hại. Nếu upstream cho `TokenManager` đọc `headers.Cookie` thì **patch hở âm thầm** — không lỗi, chỉ là request dùng cookie cũ. Đây là mục 5 của `PATCH_NOTES.md`.

### Không dùng `POST /update_cookie` của upstream

Upstream đã có endpoint này (`app/api/endpoints/hybrid_parsing.py:56`), nhìn như đường tắt. Loại vì nó `print()` cookie 3 lần, và vì nó là push-model — ta cần pull-model (parser đọc Redis) để cookie đổi mà không cần ai gọi ai.

---

## 1.4 `PATCH_NOTES.md` — đúng 5 mục

Đặt ở gốc fork. Mỗi lần pull upstream, chạy lại checklist này:

1. File mới `crawlers/douyin/web/cookie_provider.py`
2. Một dòng trong `get_douyin_headers()` — nguồn của `Cookie` header
3. Ba `print()` đã xoá trong `update_cookie()` — **kèm lý do**, để đừng ai merge chúng về
4. `config.yaml` → `Cookie: ""`, fallback chuyển sang env
5. **Cảnh báo:** `utils.py` có bản copy config riêng. Mỗi lần pull phải `grep -n 'headers\["Cookie"\]\|headers.Cookie' crawlers/douyin/web/utils.py` — nếu có hit thì patch đã hở

---

## 1.5 Tests (pytest, trong fork)

Bốn test này là tiêu chí hoàn thành của `IDEA.md §4.3`, map 1:1:

| Test | Kiểm |
|---|---|
| `test_cookie_from_redis` | Set `douyin:cookie:current` → request kế tiếp dùng cookie mới, **không restart process** |
| `test_fallback_when_no_key` | Xoá key → dùng `DOUYIN_COOKIE_FALLBACK` |
| `test_no_crash_when_redis_down` | Redis unreachable → không crash, log warning **không kèm giá trị**, dùng fallback |
| `test_cookie_never_in_logs` | Capture toàn bộ log của một vòng request → assert không chứa cookie value, không chứa `sessionid`/`passport_` |

Cộng một check trong CI: `git diff upstream/main --stat` ≤ **50 dòng**.

> **As-built:** bốn test trên đã đổi tên khi implement (tách sync/async, parametrize theo nguồn cookie) và thành **19 test function / 20 test case**. Map: `test_cookie_from_redis` → `test_sync_get_cookie_from_redis` + `test_async_cookie_from_redis`; `test_fallback_when_no_key` → `test_sync_resolve_cookie_fallback_when_no_key` + `test_async_fallback_when_no_key` (+ `..._when_key_deleted` cho trường hợp Redis sống nhưng key bị xoá); `test_no_crash_when_redis_down` và `test_cookie_never_in_logs` giữ tên, cái sau parametrize `[redis]` / `[env_fallback]`. Con số ≤50 dòng: xem §As-built lệch 1.

---

## 1.6 Secret scan vào CI

`gitleaks` hoặc `trufflehog` chạy trên mọi PR, **bao gồm cả submodule**. Đây là thứ đáng ra phải bắt được cookie ở §7.2 phát hiện 4.

---

## Definition of done

- [x] Cookie cũ đã thu hồi (nếu fork public), `config.yaml` scrub thành `Cookie: ""` — không cần thu hồi (upstream), vẫn scrub phòng tương lai
- [x] `git init` xong, fork là submodule, `git diff upstream/main` chạy được — outer commit `b698013`, fork commit `5fe4e4d`
- [x] 4 pytest xanh — **20 pytest passing** (thêm 16 từ review findings)
- [x] `git diff upstream/main --stat`: modified upstream lines 37/50; new code 121/250; docs+tests 883 (ungated) — xem §Deviation
- [x] `PATCH_NOTES.md` có đúng 5 mục
- [x] Secret scan trong CI, xanh — thêm custom rule + canary step, xem §As-built
- [x] **Chứng minh bằng tay:** `scripts/prove-cookie-swap.py` passes — cookie thay đổi mid-process qua redis-py real client, không restart

---

## As-built / Lệch so với kế hoạch

### Lệch 1 — cách đo "≤ 50 dòng"

DoD viết `git diff upstream/main --stat ≤ 50`. Con số đó **không phân biệt loại file**: patch vào file upstream sở hữu, `cookie_provider.py` của ta, `PATCH_NOTES.md`, test mới — tất cả cộng vào một số. Hiểu theo đúng chữ thì budget hết trước khi viết dòng test đầu tiên, và nó đẩy test ra khỏi fork — đúng chỗ mà §1.5 yêu cầu đặt test.

`IDEA.md:158` viết cùng tiêu chí này là "**~50 dòng thay đổi**" — tức *lines changed*, tức **bề mặt merge conflict**. Đó mới là thứ budget bảo vệ.

`scripts/check-patch-size.sh` vì vậy gate ba loại riêng:

| Loại | Thực tế | Gate | Vì sao |
|---|---|---|---|
| Dòng sửa trong file upstream cũng sở hữu | **37** | ≤ 50 | Bề mặt merge conflict thật — ý định gốc của DoD |
| Code mới của riêng ta (`cookie_provider.py`, …) | **121** | ≤ 250 | Để "dồn logic vào file của ta" không lách được budget mãi |
| Test + docs | **883** | không gate | Upstream không có bản nào ⇒ không thể conflict. Gate cái này là phạt đúng hành vi mình muốn |

Cả ba xanh. Gate đã kiểm hai chiều: hạ limit thì fail, ở limit thật thì pass.

### Lệch 2 — Phase 1 chạy trước Phase 0

`blockedBy: [0]` nhưng Phase 0 chưa chạy. Ba câu của Gate A (voice clone, endpoint Reels, `expires_at`) không chạm output của Phase 1, nên không câu trả lời nào bắt Phase 1 viết lại. **Gate A vẫn chặn từ Phase 3.**

### Năm thứ code review tìm ra — bản đầu pass test nhưng vẫn hở

Bản implement đầu có 5 test xanh và vẫn còn 5 lỗ. Mỗi lỗ giờ có test giữ, và mỗi test đã kiểm bằng **mutation**: cố tình phá invariant, bắt buộc suite phải đỏ.

1. **Secret scan không nhìn thấy cookie Douyin.** Ruleset mặc định của gitleaks không có rule nào cho cookie. Nó bắt được cookie upstream **chỉ vì** 3 trong ~40 field tình cờ là base64 entropy cao (`fpk1`, `bd_ticket_guard_client_data`, `bit_env`); một cookie hex thường (`sessionid=`, `sid_guard=`, `passport_csrf_token=`) cho **0 findings**. Đây là lỗi suy luận lúc verify: "scan bắt được cookie cũ" **không** chứng minh "scan bắt được cookie".
   → Thêm rule `douyin-session-cookie` trong `.gitleaks.toml`, và một **bước canary trong CI** tự trồng cookie mỗi lần build rồi fail nếu scanner không phát hiện được. Đây là **bước CI, không phải pytest** — ở `.github/workflows/ci.yml`.

2. **`update_cookie()` ghi cookie của caller vào file tracked, không auth.** `POST /api/hybrid/update_cookie` → `yaml.dump` vào `config.yaml`. Xoá 3 `print()` chỉ đóng đường log; đường ghi đĩa và đường thay cookie vẫn mở.
   → Xoá hẳn method. Endpoint upstream giờ không chạy — cố ý. Test: `test_update_cookie_method_removed`.

3. **Gọi Redis sync trong `async def` làm nghẽn event loop.** Đo được: 5 call đồng thời mất 5.03s (tuần tự hoàn toàn), 0 nhịp heartbeat của loop. Redis down là bỏ đói mọi request Douyin **và cả health endpoint của parser** ⇒ health gate ở Phase 2 sẽ chẩn đoán sai "Redis chết" thành "parser chết".
   → Dùng `redis.asyncio`. Test: `test_concurrent_headers_not_serialized_by_slow_redis`.

4. **Cache async client dùng chung qua nhiều event loop.** Lỗi do chính bản sửa (3) tạo ra, và **suite 16 test không bắt được vì fixture reset `_async_client` trước mỗi test** — fixture che mất bug. Client `redis.asyncio` gắn với loop tạo ra nó; loop đóng là mọi call sau raise `RuntimeError`, `get_cookie_async()` nuốt lỗi, cookie **đóng băng vĩnh viễn** ở giá trị rỗng, cả process, chỉ log đúng một warning. Bắt được bằng `scripts/prove-cookie-swap.py` (hai `asyncio.run()` liên tiếp), không phải bằng test.
   → Cache theo từng loop, dựng lại khi loop khác. Test: `test_get_cookie_async_rebuilds_client_across_event_loops` (test này **không** reset `_async_client` — đó là toàn bộ ý nghĩa của nó).

5. **`_warned` latch vĩnh viễn, và chuỗi cookie rỗng im lặng.**
   - Latch: sau lần lỗi đầu tiên của cả process, mọi outage sau đó vô hình trong log, trong khi vẫn tốn thời gian mỗi request. Đèn "Health bar: cookie" ở plan §7 sẽ không có dữ liệu sau phút đầu.
     → Nén theo thời gian (60s) + counter `_consecutive_failures`. Test: `test_warning_suppressed_within_window`, `test_warning_reemitted_after_window_elapses`, `test_consecutive_failures_counter_increments_and_resets`.
   - Chuỗi rỗng: Redis miss + không có fallback ⇒ gửi `Cookie` rỗng, không log gì. Trong tỉ lệ job `blocked`, "ta không gửi cookie" và "Douyin từ chối cookie" giống nhau nhưng cách sửa ngược nhau.
     → Warning riêng, **không kèm giá trị**. Test: `test_resolve_cookie_warns_when_chain_fully_empty`, `test_resolve_cookie_async_warns_when_chain_fully_empty`, `test_resolve_cookie_does_not_warn_when_config_value_present`.

Thêm `test_sync_client_gets_load_bearing_timeouts` / `test_async_client_gets_load_bearing_timeouts`: `socket_timeout=1` được `PATCH_NOTES` gọi là load-bearing nhưng trước đó **không có gì bắt buộc** — xoá cả hai kwarg vẫn pass toàn bộ suite.

**Tổng: 19 test function → 20 test case** (`test_cookie_never_in_logs` parametrize 2 nguồn; kế hoạch yêu cầu 4). CI chạy lại mutation mỗi build, nên sửa đổi sau này làm assertion mất hiệu lực sẽ bị bắt.
