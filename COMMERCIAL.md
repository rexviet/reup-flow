# COMMERCIAL.md — Chuyển từ bản internal sang bản thương mại

Tài liệu này là **phần tiếp theo của `IDEA.md`**, không thay thế nó.

`IDEA.md` mô tả bản nội bộ: tất cả chạy trên server của bạn, một tệp người dùng, không ai trả
tiền. Tài liệu này mô tả cách biến nó thành sản phẩm bán được theo mô hình **hybrid** — phần
lớn chạy trên máy khách, một lát mỏng giá trị cao nằm trên server bạn.

**Thứ tự thực hiện: hoàn thành M1–M2 của `IDEA.md` trước.** Bản internal chạy được là điều kiện
tiên quyết. Đừng nhảy thẳng vào đây — bạn sẽ vừa debug logic nghiệp vụ vừa debug ranh giới
client/server cùng lúc.

---

## 0. Quyết định phải chốt trước khi code

Có **hai biến thể hybrid**. Chúng khác nhau ở một câu hỏi duy nhất: *ai gọi API Douyin?*

### Variant A — Signing oracle (client gọi Douyin)

Server chỉ ký request. Client tự gọi Douyin bằng IP và cookie của chính khách.

- Không cần proxy pool. Rủi ro IP tự phân tán theo từng khách
- Cookie không rời máy khách → giảm hẳn trách nhiệm pháp lý về credential
- Chi phí server gần như bằng 0, scale theo số khách chứ không theo lưu lượng
- Client phức tạp hơn: phải tự gọi HTTP, parse, xử lý lỗi Douyin

### Variant B — Parser trên server (server gọi Douyin)

Giữ nguyên parser service như `IDEA.md`. Client chỉ có UI + tải file từ CDN.

- Client đơn giản hơn nhiều, ra sản phẩm nhanh hơn
- Video vẫn không đi qua server bạn (chỉ JSON metadata) → bandwidth vẫn ổn
- **Cái giá:** phải nuôi proxy pool, và phải giữ cookie của khách trên hạ tầng của bạn

### Chọn cái nào

| Nếu... | Chọn |
|---|---|
| Nhắm 20–50 khách doanh nghiệp, cần ra mắt nhanh | **B** |
| Nhắm số lượng lớn khách nhỏ / self-serve | **A** |
| Không muốn giữ credential của người khác | **A** |
| Team nhỏ, không muốn vận hành proxy pool | **A** |

Phần còn lại của tài liệu viết cho **Variant A**. Chỗ nào Variant B khác thì có ghi chú
`[Variant B]`.

---

## 1. Bản đồ di trú component

So với `IDEA.md`:

| Component (IDEA.md) | Internal | Thương mại (Variant A) |
|---|---|---|
| Parser service (fork) | Server | **Xẻ đôi**: ký ở server, gọi + parse ở client |
| Orchestrator API | Server | **Client** — thành job manager local |
| Job worker | Server | Client |
| Frontend | Server phục vụ | Client (UI của app/extension) |
| Chrome extension | Client | Client, gần như không đổi |
| Cookie store (Redis) | Server | Client — cookie không rời máy khách |
| Postgres | Server | SQLite local |
| Object storage | Server | Ổ đĩa khách |
| *(mới)* Control plane | — | **Server** |

**Điểm dễ nhầm nhất:** `Orchestrator API` nghe rất "backend" nhưng nó quản job state, tiến độ,
resume, dedupe — toàn bộ là dữ liệu về các file *trên máy khách*. Giữ nó ở server nghĩa là lưu
state mình không cần và phải đồng bộ ngược một sự thật chỉ client mới biết chắc.

Cách nghĩ gọn: **kiến trúc thương mại gần như là ảnh gương của bản internal — tất cả chạy về
client, trừ một lát mỏng của parser.**

---

## 2. Server — Control plane

Đây là service duy nhất bạn vận hành. Nhỏ, không state nghiệp vụ, dễ scale.

### 2.1 Signing oracle

**Nguyên tắc thiết kế quan trọng nhất của cả tài liệu này:** server trả về **toàn bộ recipe của
request**, không phải chỉ chuỗi chữ ký.

```
POST /v1/sign
Authorization: Bearer <license_token>
{
  "op": "user_post",                     // user_post | aweme_detail | user_profile
  "args": { "sec_user_id": "MS4wLj...", "max_cursor": 0, "count": 20 },
  "client_version": "1.4.2"
}

200
{
  "url": "https://www.douyin.com/aweme/v1/web/aweme/post/?device_platform=...&a_bogus=...",
  "method": "GET",
  "headers": { "User-Agent": "...", "Referer": "https://www.douyin.com/" },
  "response_hint": { "list_path": "aweme_list", "cursor_path": "max_cursor" }
}
```

Vì sao trả cả recipe: Douyin không chỉ đổi thuật toán, đôi khi nó thêm param bắt buộc hoặc đổi
header. Nếu client tự ghép URL thì loại thay đổi đó bắt bạn build lại và phân phối lại binary.
Nếu server quyết định luôn hình dạng request, client chỉ là executor, và **hầu hết sự cố fix
được ở phía server mà khách không phải cập nhật gì.**

`response_hint` đẩy nốt phần path trong JSON lên server — cho phép bạn thích ứng cả khi Douyin
đổi tên field.

Client **không bao giờ** tự ghép URL Douyin. Nếu thấy string `douyin.com/aweme` hardcode trong
code client, đó là bug kiến trúc.

`[Variant B]` Thay endpoint này bằng chính parser service như `IDEA.md`, thêm auth + metering ở
trước. Bỏ toàn bộ mục 2.1 này.

### 2.2 License & metering

```
POST /v1/license/activate    { key, device_fingerprint } -> { token, expires_at, entitlements }
POST /v1/license/heartbeat   { token } -> { valid, entitlements, quota_remaining }
GET  /v1/version/manifest    -> { latest, min_supported, download_url, changelog_url }
POST /v1/telemetry           { client_version, op, outcome, error_code }
```

- Token ngắn hạn (24h), refresh qua heartbeat. Không dùng license key trực tiếp cho mọi request
- Mỗi lần `/v1/sign` trừ một đơn vị quota → hook billing nằm sẵn ở đây
- `/v1/telemetry` **là hệ thống cảnh báo sớm.** Tỉ lệ lỗi tăng đột biến = Douyin vừa đổi gì đó.
  Dựng alert trên metric này ngay từ ngày đầu, đừng để đến khi khách báo

### 2.3 Vì sao mô hình này chống crack được

Client **về mặt vật lý không biết cách tính chữ ký**. Không có hàm `checkLicense()` nào để patch
— decompile hết cũng chỉ thấy vài dòng gọi HTTP. Đây là tính chất mà license check nhúng trong
binary không bao giờ có.

### 2.4 Chống lạm dụng

Rủi ro: một khách mua license rồi proxy chữ ký cho mười người khác.

- Rate limit theo license (đặt trần cao hơn nhu cầu thật ~3 lần)
- Ràng token với `device_fingerprint`; giới hạn số device đồng thời
- Phát hiện bất thường: số `sec_user_id` khác nhau trong ngày, phân bố theo giờ, tần suất
- Có nút thu hồi license tức thì — và nó **thực sự có hiệu lực**, khác hẳn binary đã bán đứt

Không chặn tuyệt đối được, chỉ cần đủ để không đáng công.

### 2.5 Yêu cầu vận hành

**Server chết là tất cả khách chết.** Không lách được — không thể pre-cache chữ ký vì chúng gắn
với từng request cụ thể.

→ Tách `/v1/sign` thành service riêng, cực đơn giản, deploy độc lập với dashboard và billing.
Nó là hàm thuần, dựng nhiều instance sau load balancer. Đừng để nó chung số phận với phần còn
lại.

Mục tiêu: p99 < 150ms, uptime 99.9%.

---

## 3. Client

### 3.1 Hai hình dạng

**Extension-only** — dùng cho bản thương mại đầu tiên.

Extension đã có host permission cho `douyin.com`, nên `fetch` từ context của nó **tự động kèm
cookie và không dính CORS** — bỏ luôn được bước harvest rồi gắn cookie thủ công của `IDEA.md`.
`chrome.downloads` lo phần tải file. Không có bước cài đặt.

Hạn chế: ít kiểm soát concurrency và resume, file rơi vào thư mục Downloads mặc định.

**Desktop app (Tauri) + extension** — tier cao hơn.

Extension chỉ còn nhiệm vụ đẩy cookie sang `localhost` của app. App lo tải song song, resume tử
tế, chọn thư mục lưu, quản lý thư viện. Tauri thay vì Electron vì binary nhỏ hơn hẳn.

Ship extension-only trước để kiểm chứng có người trả tiền không. Kiến trúc không đổi giữa hai
bản — chỉ cái executor phía client thay hình dạng.

### 3.2 Đổi gì so với IDEA.md

| Trong IDEA.md | Thành |
|---|---|
| Postgres | SQLite local, schema giữ nguyên |
| Redis cookie store | Không cần — extension đọc cookie tại chỗ khi gọi |
| SSE endpoint | Event trong tiến trình / `chrome.runtime` message |
| Gọi parser service qua HTTP | Gọi `/v1/sign` → tự `fetch` Douyin → tự parse |
| `X-Api-Key` nội bộ | License token |
| Object storage | Filesystem API / `chrome.downloads` |

### 3.3 Port nguyên, không viết lại

Những thứ này chuyển sang gần như y nguyên, chỉ đổi chỗ chạy:

- Logic **enumerate hai pha** và rate limit khác nhau cho từng pha (mục 3.1 của `IDEA.md`)
- Checkpoint `max_cursor` sau mỗi trang
- **Không cache `play_addr`** — re-resolve ngay trước khi tải
- Dedupe theo `aweme_id`, incremental
- Retry per-video, trạng thái `blocked` vs `failed`
- Parse `sec_user_id` từ URL, follow short link

Đây là lý do phải làm xong M1–M2 của `IDEA.md` trước: toàn bộ phần khó của nghiệp vụ đã được
kiểm chứng, giai đoạn này chỉ là di dời.

### 3.4 Xử lý mất kết nối control plane

- Job đang chạy giữa chừng: các video đã resolve URL vẫn tải xong được
- Không xin được chữ ký mới → job vào trạng thái `paused_no_signature`, giữ checkpoint
- UI báo rõ "mất kết nối máy chủ", **không** báo lỗi chung chung
- Tự retry với backoff; job tiếp tục khi có lại kết nối

---

## 4. Nghĩa vụ pháp lý & giấy phép

### 4.1 Apache 2.0

Repo upstream dùng Apache-2.0 — cho phép rõ ràng dùng thương mại và phân phối dạng binary,
**không copyleft** nên code bạn viết thêm không phải mở.

Nghĩa vụ khi phân phối client:

- [ ] Kèm bản `LICENSE` Apache 2.0 trong bộ cài
- [ ] Giữ copyright notice của tác giả gốc; kèm `NOTICE` nếu upstream có
- [ ] **§4(b): nêu rõ các file đã sửa đổi** — chính là `PATCH_NOTES.md` từ `IDEA.md`, đóng gói kèm
- [ ] Mục "Third-party licenses" trong màn hình About

Lưu ý: `[Variant A]` phần thuật toán ký chạy trên server bạn, **không phải hành vi phân phối**.
Apache không có điều khoản network-use như AGPL, nên phần đó không phát sinh nghĩa vụ.

### 4.2 Rà soát dependency

- [ ] Chạy `pip-licenses` và `license-checker` (npm) trên **toàn bộ** cây phụ thuộc
- [ ] Chỉ cần một lib GPL lọt vào client là phát sinh nghĩa vụ mở source
- [ ] Đưa bước này vào CI, fail build nếu xuất hiện license không nằm trong allowlist

### 4.3 Rủi ro không nằm ở code

Tự dùng nội bộ và bán ra thị trường là **hai mức rủi ro khác hẳn nhau**. ToS Douyin cấm crawl tự
động; video thuộc bản quyền creator. Bán công cụ tải hàng loạt nội dung của người khác có thể bị
xem là tiếp tay xâm phạm, tùy khu vực pháp lý.

**Trước khi đầu tư nghiêm túc: hỏi ý kiến luật sư sở hữu trí tuệ.** Tài liệu này không phải tư
vấn pháp lý.

### 4.4 Positioning ít rủi ro hơn

Cùng codebase, khác cách đóng gói và khác tệp khách:

> Công cụ backup nội dung cho **chính chủ tài khoản** — creator tự lưu trữ video của mình, hoặc
> agency quản lý account có ủy quyền.

Với hướng này bạn còn dùng được **Open API chính thức của Douyin** cho phần metadata và analytics
(vì chính chủ authorize được qua OAuth), chỉ dùng đường web cho phần tải file. Sản phẩm ổn định
hơn, câu chuyện bán hàng sạch hơn hẳn.

Nếu chọn hướng này, cân nhắc bổ sung: xác nhận quyền sở hữu khi tạo job, log audit, ToS yêu cầu
khách cam kết chỉ tải nội dung mình có quyền.

---

## 5. Lộ trình

Điều kiện vào: `IDEA.md` M1–M2 đã xong và chạy ổn định.

**C1 — Tách signing oracle**
Bóc phần ký ra khỏi fork thành service độc lập. Bản internal chuyển sang gọi service này thay vì
tính tại chỗ. *Chưa có client mới, chưa có license.* Chứng minh ranh giới đúng trước khi xây gì
lên trên.

**C2 — Control plane**
License activate/heartbeat, quota, version manifest, telemetry. Dashboard admin tối thiểu: tạo
license, xem usage, thu hồi.

**C3 — Client extension**
Port orchestrator + worker sang extension. SQLite (hoặc IndexedDB) local. Gọi `/v1/sign`.
`chrome.downloads`. Đây là milestone nặng nhất.

**C4 — Đóng gói thương mại**
Onboarding nhập license key. Trang thanh toán. Tuân thủ mục 4.1–4.2. Trang trạng thái dịch vụ.

**C5 — Desktop app** *(tùy nhu cầu)*
Tauri client cho tier cao. Chỉ làm khi có khách thực sự yêu cầu.

---

## 6. Tiêu chí nghiệm thu

- [ ] Client không chứa thuật toán ký; grep `a_bogus` trong bundle client ra rỗng
- [ ] Client không hardcode URL endpoint Douyin nào
- [ ] Cookie không bao giờ được gửi tới server bạn (kiểm bằng network capture)
- [ ] Thu hồi license → client ngừng hoạt động ở request ký kế tiếp
- [ ] Douyin đổi param bắt buộc → giả lập bằng cách sửa recipe ở server, client cũ vẫn chạy
      **không cần update**
- [ ] Tắt control plane giữa job → job vào `paused_no_signature`, bật lại thì chạy tiếp từ
      checkpoint
- [ ] Video không đi qua hạ tầng của bạn (kiểm log egress)
- [ ] CI fail khi có dependency ngoài license allowlist
- [ ] Bộ cài chứa LICENSE, NOTICE, PATCH_NOTES

---

## 7. Câu hỏi cần chốt

Không phải việc của coding agent, nhưng chặn thiết kế:

1. **Variant A hay B?** Xem mục 0. Quyết định này ảnh hưởng gần như mọi thứ phía sau
2. **Mô hình giá:** thuê bao theo tháng, hay theo số video? Metering ở mục 2.2 hỗ trợ cả hai,
   nhưng cần chốt trước khi làm C4
3. **Positioning:** công cụ tải chung, hay backup cho chính chủ (mục 4.4)? Ảnh hưởng tới tính
   năng, ToS, và mức rủi ro pháp lý
4. **Cam kết vận hành:** bạn hứa fix trong bao lâu khi Douyin đổi thuật toán? Con số này phải
   nằm trong ToS, và phải là con số bạn giữ được lúc 2 giờ sáng
