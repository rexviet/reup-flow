# Admin dashboard: brainstorm cho UI/UX

> **Đây là bản dựng lại sau khi đã làm.** Plan gốc đi thẳng vào lập kế hoạch, không có brainstorm
> riêng. Tài liệu này tổng hợp từ `plans/261111-1644-admin-dashboard/` (plan.md, phase 3, 4, 6),
> hai journal ngày 2026-11-11, `docs/admin-dashboard.md` và các quyết định đã chốt với người dùng
> (2026-11-11 và 2026-11-12). Phần "Gợi ý thiết kế" và "Câu hỏi mở" là đề xuất mới, chưa ai duyệt.
> Trạng thái code: giao diện đã có bản đầu ở `services/web/src/dashboard/`, mới test với API giả,
> chưa kiểm tra bằng trình duyệt trên stack thật.

## 1. Vấn đề

Quy trình làm một video đang rải ở ba nơi: tab Jobs (tải từ Douyin), tab Review (duyệt phụ đề),
tab Workspace 2D (xem pipeline), cộng Studio và Temporal UI. Muốn biết một video đang kẹt ở
đâu, admin phải mở nhiều trang và tự ghép thông tin.

**Mục tiêu:** một trang duy nhất, mỗi video một dòng, nhìn là biết video đang ở giai đoạn nào,
có gì cần mình làm, và làm được ngay tại đó.

## 2. Người dùng và bối cảnh

- **Một admin duy nhất**, dùng trên máy Mac chạy hệ thống (localhost). Không có vai trò, không có đăng nhập riêng.
- Có thể xem từ xa qua cloudflared tunnel, bảo vệ bằng Cloudflare Access. Màn hình điện thoại và cửa sổ hẹp phải dùng được.
- Công việc điển hình: quét xem hôm nay có gì kẹt, chọn video nào đưa vào localize, nghe thử rồi duyệt, tải bản cuối để đăng.
- Ngôn ngữ giao diện: tiếng Việt.

## 3. Kết quả mong muốn

- Mở trang là thấy ngay: hệ thống có khỏe không, có gì cần xử lý, bao nhiêu video ở mỗi giai đoạn.
- Mỗi video luôn hiện đúng giai đoạn **hiện tại**, kể cả bước con khi đang chạy.
- Quy trình chính chạy trọn trên một trang: giữ video, đưa vào localize, theo dõi, xem bản gốc và bản dịch cạnh nhau, duyệt, nhận MP4 và SRT.
- Thao tác tốn tiền hoặc tốn thời gian máy không thể bấm nhầm.

## 4. Nguyên tắc thiết kế (rút từ các quyết định đã chốt)

1. **Một nguồn sự thật cho giai đoạn.** Giai đoạn tính bằng một hàm duy nhất ở server; UI chỉ hiển thị, không tự suy luận.
2. **Trung thực khi thiếu dữ liệu.** Nếu Temporal hoặc Postgres không đọc được, giai đoạn hiện kèm dấu `?`, không bao giờ hiện "rảnh". Số liệu sức khỏe cũ thì hiện `unknown`, không hiện như sự thật.
3. **Mọi cảnh báo kèm việc nên làm.** Mỗi mục "Cần xử lý" có lý do tiếng Việt và nút gợi ý.
4. **Thao tác an toàn khi bấm lại.** Server kiểm lại điều kiện từng video; hai tab cùng bấm không gây hỏng. UI hiện kết quả từng video (thành công hoặc bị bỏ qua kèm lý do).
5. **Duyệt gắn với thứ admin đang xem.** Nút Duyệt gửi kèm revision đang hiển thị; nếu revision đổi trước khi render, hệ thống báo thay vì dựng bản chưa duyệt.
6. **Chỉ đọc với Temporal.** Không có nút hủy run; muốn hủy thì dùng Temporal UI.
7. **Một người dùng, tối giản.** Không phân quyền, không thông báo, không thống kê chi phí ở MVP.

## 5. Phạm vi

**Có:** thanh sức khỏe, hộp "Cần xử lý", bảng pipeline có lọc và chọn nhiều, thao tác hàng loạt, trang chi tiết video, khu vực kênh và job tải, phát và tải media.

**Không có (ngoài phạm vi):** tự quét kênh, thống kê chi phí hoặc năng suất, thông báo Telegram/Slack, tự đăng qua MCP, hủy run localization, chạy lại một bước riêng lẻ, nhúng Studio vào dashboard, tab Workspace 2D (đã gỡ; bản 3D đang thiết kế ở nơi khác).

## 6. Vòng đời một video (cái UI phải thể hiện)

```
Tải ──► Đang giữ ──► Chờ chạy ──► Đang localize ──► Chờ duyệt ──► Chờ render ──► Đang render ──► Đã render ──► Đã đăng
 │         (admin       │          (chuẩn bị →          │   ▲                          │
 │         bấm đưa      │           dịch →              │   └── Đã trả về              └──► Render lỗi
 │         vào)         │           lồng tiếng →        │        (admin trả lại)
 ▼                      ▼           kiểm tra)           ▼
Tải lỗi          Gửi lỗi / Chạy lỗi                   Bỏ qua (quảng cáo, không có video)
```

Mỗi giai đoạn nên có một chip có tên và màu nhất quán; bước con chỉ hiện khi đang localize.

| Giai đoạn | Ý nghĩa với admin |
|---|---|
| Đang tải / Tải lỗi | job tải chưa xong hoặc thất bại |
| Đang giữ | tải xong, chờ admin quyết định đưa vào localize (video từ job kênh mặc định vào đây) |
| Chờ chạy | đã xếp hàng, chưa thấy workflow chạy |
| Đang localize + bước con | chuẩn bị, dịch, lồng tiếng, kiểm tra; bước lạ hiện "không rõ" |
| Gửi lỗi / Chạy lỗi | đầu vào hỏng hoặc run thất bại |
| Bỏ qua | quảng cáo hoặc không có video |
| Chờ duyệt | pipeline xong, đợi admin xem |
| Đã trả về | admin trả lại để sửa tiếp |
| Chờ render / Đang render / Render lỗi | đã duyệt; worker trên máy dựng bản cuối |
| Đã render | có MP4 và SRT |
| Đã đăng | admin đánh dấu tay, kèm platform và link bài đăng |

**Khái niệm "generation":** mỗi lần chạy lại là một generation mới (g1, g2, g3…) cho cả run. Dashboard luôn
hiện generation hiện tại; trang chi tiết liệt kê tất cả. Admin không cần hiểu cách tính, nhưng nhìn thấy
"g7" cạnh video và biết đây là lần chạy thứ mấy.

## 7. Cấu trúc màn hình

Một trang chính và một trang chi tiết (`#/video/<aweme_id>`, định tuyến bằng hash).

### 7.1 Trang chính, từ trên xuống

1. **Thanh sức khỏe** (poll 10 giây). Badge: Cookie Douyin, VoiceStudio, voice worker (stt/tts), worker review trên máy, Temporal, MinIO, đĩa. Màu theo `ok / degraded / down / unknown`, mỗi badge có tooltip nêu lý do. Đĩa dưới 20 GB là `degraded`, dưới 5 GB là `down`. Worker review mất heartbeat quá 2 phút là `down`; khi đó VoiceStudio và đĩa chuyển `unknown`.
2. **Cần xử lý.** Danh sách gọn, mỗi dòng: video, lý do, nút thao tác gợi ý. Job tải bị chặn hoặc lỗi hiện riêng ở đầu hộp.
3. **Pipeline.**
   - Hàng chip đếm theo giai đoạn; bấm để lọc.
   - Ô tìm kiếm (theo mã video và mô tả), lọc theo kênh và theo ngày.
   - Bảng, có checkbox chọn nhiều. Cột: video (mã, mô tả, kênh), generation, giai đoạn (kèm bước con), thời gian ở giai đoạn này, cập nhật lần cuối.
   - Thanh thao tác hàng loạt xuất hiện khi có chọn: Đưa vào localization, Chạy lại, Duyệt, Render lại, Đánh dấu đã đăng.
   - Poll 3 giây, tạm dừng khi tab ẩn.
4. **Kênh và tải.** Form tải link hoặc kênh có công tắc "Tự localize" (mặc định tắt) và ô "Localize ngay" cho link video đơn; danh sách job (Thử lại, Hủy); danh sách kênh với công tắc tự localize.

### 7.2 Trang chi tiết video

- Video gốc và bản cuối đặt **cạnh nhau** (link ký lấy từ server, làm mới trước khi hết hạn 10 phút; tua được).
- Timeline các bước (từ Temporal), verdict của Reviewer và số mismatch (cue lời đọc lệch phụ đề), chi phí LLM.
- Danh sách generation.
- Nút theo giai đoạn: Duyệt, Trả về (kèm ghi chú), Render lại, tải MP4 và SRT, Đánh dấu đã đăng (nhập link).
- **Mở Studio**: tạo yêu cầu, hiện trạng thái xử lý (đang chờ, xong, lỗi). Ghi chú bắt buộc: "Studio mở trên máy chạy dashboard", kể cả khi xem qua tunnel.
- Lịch sử thao tác.

## 8. Thao tác: khi nào bật nút nào

| Thao tác | Điều kiện để bật | Ghi chú cho UI |
|---|---|---|
| Đưa vào localization | đang ở "Đang giữ" | đường duy nhất đi từ giữ sang chạy |
| Chạy lại | đã gửi hoặc thất bại nhiều lần, không còn workflow mở, Temporal đọc được, không đang render | tạo generation mới cho cả run; không chạy lại được video "Gửi lỗi do đầu vào hỏng" |
| Duyệt | đang chờ duyệt, không có thay đổi chưa lưu trong Studio, không đang render | **một video mỗi lần**; gửi đúng revision đang xem; còn mismatch thì phải xác nhận riêng |
| Trả về | đang chờ duyệt hoặc đã render, chưa đăng | kèm ghi chú |
| Render lại | render lỗi hoặc đã render | |
| Đánh dấu / bỏ đánh dấu đã đăng | đã render | đánh dấu chỉ có hiệu lực một lần, giữ platform và link cũ |
| Mở Studio | một video, có review project | chỉ mở trên máy host; thay Studio đang mở (có sao lưu) |

**Xác nhận:** Chạy lại và các thao tác hàng loạt tốn tiền hay thời gian máy phải có hộp xác nhận ghi rõ
số video. Duyệt khi còn mismatch xác nhận riêng, ghi rõ revision và số mismatch. Tối đa 100 video mỗi lần bấm.

**Kết quả hàng loạt:** luôn hiện "N thành công, M bị bỏ qua", kèm lý do từng video bị bỏ qua (ví dụ
"trạng thái vừa đổi", "Temporal không đọc được", "đầu vào hỏng, chạy lại không giúp được").

## 9. Hộp "Cần xử lý": các lý do và câu chữ gợi ý

| Mã | Khi nào | Việc nên làm |
|---|---|---|
| `download_failed` | tải thất bại | Thử lại job tải |
| `dispatch_failed` | đầu vào bị từ chối | Kiểm tra file gốc (chạy lại không giúp) |
| `dispatch_dead` | gửi sang localization thất bại nhiều lần | Chạy lại |
| `run_failed` | lần chạy localization thất bại | Xem timeline, Chạy lại |
| `render_failed` | render bản cuối thất bại | Render lại |
| `needs_human` | pipeline đánh dấu cần người xem | Mở chi tiết, xem và duyệt |
| `mismatch` | còn cue lời đọc lệch phụ đề | Mở Studio sửa, hoặc duyệt kèm xác nhận |
| `dirty` | project Studio có thay đổi chưa lưu | Lưu trong Studio trước khi duyệt |
| `retrying` | một activity đang thử lại lần 2 trở lên | Theo dõi |
| `stuck` | một activity chạy quá 30 phút | Xem timeline; dùng Temporal UI nếu cần hủy |
| `lost` | đã gửi hơn 10 phút nhưng Temporal không có workflow và chưa có run (ví dụ bị hủy trong Temporal UI) | Chạy lại |
| `old_generation_awaiting_review` | một generation cũ vẫn chờ duyệt, auto-open có thể mở nhầm | Xử lý hoặc trả về generation cũ |

## 10. Trạng thái đặc biệt cần thiết kế

- **Rỗng:** chưa có video; một chip giai đoạn không có video; hộp "Cần xử lý" trống (nên hiện câu trấn an, không để khoảng trắng).
- **Đang tải lần đầu và poll lỗi:** giữ dữ liệu cũ khi poll lỗi, kèm chỉ báo "dữ liệu cũ"; không xóa bảng.
- **Nguồn không đọc được:** dấu `?` cạnh giai đoạn, cộng một dải thông báo cho biết nguồn nào (Temporal hay Postgres) đang lỗi.
- **Media:** link hết hạn khi đang xem (làm mới và phát lại, không kẹt); thiếu khóa media thì server trả 503 (hiện thông báo rõ, không để player trắng); object chưa có thì 404.
- **Studio:** yêu cầu mở Studio đang chờ, đã xong, hoặc lỗi; worker trên máy tắt thì badge đỏ và nút nên báo vì sao không có tác dụng.
- **Điều kiện đổi giữa chừng:** thao tác bị bỏ qua vì trạng thái vừa đổi nên gợi ý tải lại dòng đó.
- **Màn hình hẹp:** bảng pipeline thu gọn (ví dụ thẻ thay cho bảng), thanh hàng loạt dính đáy màn hình, hai player xếp dọc trên trang chi tiết.

## 11. Ràng buộc kỹ thuật ảnh hưởng đến UX

- Web gọi API bằng đường dẫn tương đối (qua nginx), không URL tuyệt đối, để chạy được qua tunnel.
- `<video>` và link tải không gửi được API key, nên media dùng link ký ngắn hạn (10 phút); cần làm mới trước khi hết hạn.
- Poll: bảng 3 giây, sức khỏe 10 giây; số đếm theo giai đoạn đi cùng response danh sách để không cần thêm request.
- Quá trình render dùng chất lượng `delivery`, file lớn (hàng trăm MB) và render lâu; trạng thái "Đang render" nên cho biết thời gian đã chạy.
- Duyệt chỉ bật cho đúng một video: nút ở thanh hàng loạt chỉ bật khi chọn đúng một video.
- Không có thao tác hủy hoặc chạy lại từng bước; đừng thiết kế nút gợi ý điều này.

## 12. Gợi ý thiết kế (đề xuất mới, chưa duyệt)

- Phân nhóm màu giai đoạn theo ý nghĩa: trung tính cho chờ (giữ, chờ chạy), xanh dương cho đang chạy, vàng cho cần người (chờ duyệt, đã trả về), đỏ cho lỗi, xanh lá cho đã xong. Đừng chỉ dựa vào màu: kèm biểu tượng hoặc chữ.
- Hộp "Cần xử lý" đặt trên bảng và giới hạn chiều cao; mục nghiêm trọng (lỗi, kẹt) lên trước.
- Cột "thời gian ở giai đoạn này" nên đổi màu khi quá lâu để thay cho việc phải tự tính.
- Trên trang chi tiết, gom nút theo giai đoạn: chỉ hiện thao tác hợp lệ, các nút còn lại ẩn hoặc tắt kèm tooltip nêu điều kiện thiếu.
- Hộp xác nhận ghi rõ hậu quả bằng chữ cụ thể ("Chạy lại 12 video: mỗi video tạo một generation mới, tốn chi phí LLM và máy"), không dùng "Bạn có chắc không?".
- Giữ bộ lọc và vị trí cuộn khi quay lại từ trang chi tiết.

## 13. Câu hỏi mở cho người làm UI/UX

1. Bảng hay thẻ là hình thức chính cho danh sách video, đặc biệt trên màn hình hẹp?
2. Có hiện thumbnail video trong bảng không (cần thêm nguồn ảnh; hiện API chưa trả)?
3. Hai player gốc và bản cuối nên đồng bộ thời gian phát với nhau không (tính năng mới)?
4. Mismatch hiện chỉ là số; có cần danh sách từng cue lệch ngay trong dashboard, hay chỉ dẫn người dùng sang Studio?
5. "Cần xử lý" có nên cho phép bỏ qua (snooze) một mục không? Hiện chưa có trạng thái này.
6. Nên hiện chi phí LLM ở bảng hay chỉ ở trang chi tiết? (Thống kê chi phí tổng nằm ngoài phạm vi.)
7. Trạng thái phát hành đã thiết kế sẵn để việc đăng qua MCP sau này ghi vào. UI có cần chừa chỗ cho nhật ký đăng tự động ngay từ bây giờ không?

## 14. Tham chiếu

- Plan: `plans/261111-1644-admin-dashboard/plan.md` và các phase 1 đến 7.
- Mô tả hành vi hiện tại: `docs/admin-dashboard.md`.
- Mã giao diện: `services/web/src/dashboard/` (DashboardPage, HealthBar, AttentionList, PipelineTable, BulkActionBar, VideoDetail, ChannelsPanel).
- Journal: `plans/journals/2026-11-11-plan-admin-dashboard-v-red-team.md`, `2026-11-11-validate-plan-admin-dashboard.md`, và `2026-11-12-cook-admin-dashboard-phase-0{1..7}-*.md`.
