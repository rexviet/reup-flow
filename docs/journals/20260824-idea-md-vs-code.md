---
date: 2026-08-24
type: journal
component: planning / parser fork
status: resolved
---

# IDEA.md Đúng Về Kiến Trúc, Sai Về Hai Con Số

**Ngày:** 2026-08-24 21:5x
**Bối cảnh:** re-scope từ reup-pipeline (đã xoá) về downloader độc lập. Brainstorm → solution design → plan.

## Chuyện gì xảy ra

Plan cũ `260824-1128-reup-pipeline` bị xoá và tuyên bố **không còn giá trị tham khảo**. Làm lại quy trình từ đầu, chỉ dùng `IDEA.md` + code thật.

`IDEA.md` là spec tốt — kiến trúc, ràng buộc cookie, chiến lược rate limit hai pha đều đúng. Nhưng **bốn thứ chỉ lộ ra khi đọc code parser**, và ba trong số đó là bug nếu tin spec.

## Bốn phát hiện

**F4 — parser bóp mọi lỗi thành 400 giống hệt nhau.** `except Exception` → `ErrorResponseModel` với `message` mặc định tĩnh `"An error occurred."`; `str(e)` không bao giờ được truyền vào. Cookie bị ban, `a_bogus` lỗi thời, profile private, parser mất mạng — **không phân biệt được**. `IDEA.md` §6.2 nói "3 lỗi liên tiếp → `blocked`", làm thế sẽ báo `blocked` cho profile private và người vận hành đi đổi cookie vô ích. Sửa: ưu tiên tín hiệu cấu trúc (`aweme_list` rỗng + `has_more=1`, HTTP 200, chắc chắn), rồi probe một profile public sau 3× 400.

**F1 — §3.2 mâu thuẫn §3.1, và mâu thuẫn đó gây ban.** §3.2 nói không cache `play_addr`, re-resolve trước mỗi lần tải. §3.1 nói API siết ~100–150 req/giờ/IP. Nhưng `fetch_one_video` đi **cùng đường risk-control**. Profile 500 video: 25 req enumerate + **500 req resolve** = vượt 3–5× hạn mức. Và `fetch_user_post_videos` **đã trả `play_addr` sẵn** trong `aweme_list`. Cache + re-resolve phản ứng: 25 req.

**F5 — `lease` cho `videos` là chưa đủ.** Brainstorm đã bắt được video kẹt `downloading` sau crash. Bỏ sót: crash giữa **enumerate** để job đứng vĩnh viễn ở `enumerating`, và enumerate lane chỉ nhặt job `pending` nên job đó chết hẳn. Tiêu chí "kill worker → resume" sẽ pass cho download và **fail âm thầm cho enumerate**.

**F6 — `attempts` phải tăng lúc claim.** Tăng lúc fail thì crash-loop không bao giờ tăng `attempts` → claim lại vô hạn sau mỗi lần reaper nhả.

## Root cause

Cùng một dạng với bài học phase 1, dịch sang tầng khác. Phase 1: *"check pass không phải bằng chứng check hoạt động."* Lần này: **"spec mạch lạc không phải bằng chứng spec khớp code."**

`IDEA.md` đọc rất thuyết phục vì nó **tự nhất quán**. F1 chỉ lộ ra khi ghép §3.1 với §3.2 rồi **nhân ra số request thật**. F4 chỉ lộ ra khi mở `douyin_web.py` và thấy `message` không bao giờ nhận `str(e)`.

Hai phát hiện đó không cần tài năng, chỉ cần **mở file thay vì tin đoạn văn**.

## Bài học

1. **Nhân con số ra.** §3.1 và §3.2 đều đúng riêng lẻ. Sai nằm ở phép nhân mà không ai làm. Ràng buộc rate limit trong spec phải được kiểm bằng số request thật của khối lượng thật.
2. **Đọc đường lỗi của dependency, không chỉ đường thành công.** Route hoạt động đúng như spec. Cái spec không nói: khi fail, nó không nói gì cả.
3. **Lease phải phủ MỌI stage có thể crash, không chỉ stage rõ ràng nhất.** F3 bắt được download vì download "trông có vẻ lâu". Enumerate cũng lâu như thế.
4. **"Đã xoá tức là không còn giá trị tham khảo" là một chỉ thị tốt.** Làm lại quy trình từ đầu tốn ~1 giờ và tìm ra bốn thứ mà kế thừa plan cũ sẽ mang theo nguyên vẹn.

## Còn nợ

- Drizzle `FOR UPDATE SKIP LOCKED`: **chưa verify.** Là gate của phase 0. Không tin doc — test 2 transaction song song, khẳng định cái thứ hai trả rỗng ngay.
- Integration test "2 event loop trong lifecycle parser thật" mà journal phase 1 nêu: **vẫn chưa tồn tại.** Spike 2 của phase 0 là chỗ gần nhất.
- 10 mutation ở `plan.md` §5 chưa cái nào chạy. Luật: mutation match rỗng = **chưa chạy**, không phải pass. Bẫy này đã sập hai lần ở phase 1.

**Status:** DONE
**Summary:** Re-scope từ đầu sau khi plan cũ bị xoá. Đọc code parser làm mất hiệu lực hai điểm của `IDEA.md` (§3.2 re-resolve, §6.2 phát hiện blocked) và lộ hai lỗ trong thiết kế brainstorm (lease cho jobs, attempts lúc claim).
**Concerns:** Không phát hiện nào trong bốn cái đến từ đọc spec. Cả bốn đến từ mở file. Phase sau vẫn còn hai giả định chưa verify về thư viện/hợp đồng ngoài — cả hai đã thành gate phase 0.
