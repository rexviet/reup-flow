---
phase: 1
title: Repo + patch cookie cho parser fork (M0)
status: not_started
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

---

## 1.6 Secret scan vào CI

`gitleaks` hoặc `trufflehog` chạy trên mọi PR, **bao gồm cả submodule**. Đây là thứ đáng ra phải bắt được cookie ở §7.2 phát hiện 4.

---

## Definition of done

- [ ] Cookie cũ đã thu hồi (nếu fork public), `config.yaml` scrub thành `Cookie: ""`
- [ ] `git init` xong, fork là submodule, `git diff upstream/main` chạy được
- [ ] 4 pytest xanh
- [ ] `git diff upstream/main --stat` ≤ 50 dòng
- [ ] `PATCH_NOTES.md` có đúng 5 mục
- [ ] Secret scan trong CI, xanh
- [ ] **Chứng minh bằng tay:** đổi key Redis → request Douyin kế tiếp dùng cookie mới, không restart
