# Kiểm tra phụ đề gốc còn sót báo nhầm trên 7616

Trạng thái: DONE_WITH_CONCERNS. Chưa commit.

## Frame gốc cho thấy gì

Tôi đã lấy `work/7616569292510948709/r0/subtitles.json` (lần chạy g9, 134 frame, 119 frame có hộp) và `r1/edit-spec.json` (cover x5 y1359 w1064 h138) từ MinIO qua `worker-agents`. Video gốc lấy từ `source/douyin/MS4wLjABAAAAjNPQW_CtRZQtt3mzKoOUpvn_FIo0RIOz3wFsoeNz6Rc/7616569292510948709.mp4`, theo input của workflow `video-7616569292510948709-g9`. Trên mỗi ảnh cắt, viền xanh là cover và viền đỏ là hộp mô hình trả về.

Ảnh cắt nằm ở `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/07f62d39-b5c7-4084-a633-0d769a53fb9d/scratchpad/crops/`. Các file là `7616-00000.png` … `7616-62000.png`, đặt tên theo mili giây, cắt từ y=1250 tới y=1750.

- Ở 0,5 s và 2 s, chữ "30岁" nằm ở y≈1385–1465, trong cover. Hộp của mô hình bắt đầu ở 1444–1453, tức thấp hơn khoảng 70–80 px. Chiều ngang của hộp thì đúng.
- Ở 21 s và 45 s, dòng phụ đề dài vẫn nằm ở y≈1385–1465, trong cover. Hộp vẫn lệch xuống khoảng 80 px theo đúng kiểu đó.
- Vì vậy độ lệch không chỉ có ở mấy giây đầu. Hộp dải y≈1445–1470 xuất hiện suốt video: ở 9,5 s, 11,5 s, 13 s, 20–23,5 s, 39 s, 43,5–46,5 s, 59 s, 62 s và 65–66 s. Chúng xen kẽ với các frame có hộp đúng chỗ, ví dụ 0 s trong cover, 0,5 và 1 s ngoài, 1,5 s trong, 2–3 s ngoài, 4 s trong, dù cả đoạn chỉ hiện một chữ "30岁" đứng yên. Finding chỉ nêu 4 thời điểm đầu, nên trông như lỗi chỉ ở đầu video.
- Dải mà rule cũ báo có 47 trên 119 frame (39%). Lý do là `_same_row` nối dải 1450 với các hộp lệch xa hơn (1545–1700, ví dụ 25,5–29 s; lần trước đã kiểm frame 27,5 s và chữ nằm trong cover).

## Các tín hiệu được đề xuất và kết luận

- "Lệch chỉ ở giây đầu": sai theo dữ liệu, như đã nêu ở trên.
- "Lệch khỏi hộp đồng thuận": cover được dựng từ chính `region` đồng thuận, ở đây region y1364 h128 và cover y1359 h138. Vì vậy mọi dải nằm ngoài cover đều lệch khỏi đồng thuận do cách dựng, và tín hiệu này không thêm thông tin gì.
- "Chồng lên cover theo chiều dọc một phần lớn" và "chiều cao giống cover": hộp lệch 80 px của mô hình có hình học y hệt một dòng thật mà cover đặt cao hơn 80 px. Ví dụ hộp y1453 h109 chồng 40% lên cover, còn dòng thật y1378 h111 dưới cover dịch lên 80 px chồng 35%. Dữ liệu hộp không phân biệt được hai trường hợp này bằng hình học.
- "Tính liên tục theo thời gian" là thứ phân biệt được. Một dòng phụ đề in cứng đứng yên trong lúc nó hiện, nên không thể ở ngoài cover rồi quay lại trong cover sau nửa giây. Nếu cover hụt thật, mọi frame của dòng đó đều ở ngoài. Mô hình thì đặt sai rồi lại đúng giữa các frame liền nhau.

## Quy tắc mới

Thay đổi nằm trong `services/localization/src/localization/agents/review_checks.py`. Có thêm helper `_sustained`, `residual_findings` gọi helper này, và comment của các hằng số được viết lại.

- Một frame ngoài cover chỉ được tính khi nó nằm trong một chuỗi frame liên tiếp ngoài cover kéo dài ít nhất `RESIDUAL_MIN_MS` (2 s, tức 4 frame). Frame có hộp nằm trong cover sẽ ngắt chuỗi. Frame không có hộp thì không ngắt, vì nó không thấy chữ hoặc câu trả lời không dùng được.
- Phần còn lại giữ nguyên: ngưỡng ngoài cover là hơn 50% diện tích không bị che, dải được gom theo hàng, cần ít nhất `RESIDUAL_SHARE = 1/3` số frame có hộp, và định dạng finding không đổi. `RESIDUAL_MIN_MS` trước đây là "các frame trải dài 2 s", giờ là "liên tục 2 s". Không có hằng số mới.

Bằng chứng định lượng (share của dải lớn nhất, run là số frame tối thiểu của một chuỗi):

| Trường hợp | Rule cũ (run 1) | Run 3 | Run 4 (chọn) |
|---|---|---|---|
| 7616 thật | 0,39 (báo nhầm) | 0,19 | 0,17 |
| 7616 cover dịch lên 40 | 0,41 | 0,24 | 0,22 |
| 7616 cover dịch lên 80 / 150 | 0,64 / 0,72 | 0,64 / 0,72 | 0,61 / 0,72 |
| 7616 nửa sau đẩy xuống 160 | 0,45 | 0,42 | 0,42 |
| 7618 thật | 0,07 | 0,01 | 0,01 |
| 7618 cover dịch lên 40 | 0,32 | 0,18 | 0,11 |
| 7618 cover dịch lên 80 / 150 | 0,53 / 0,88 | 0,41 / 0,88 | 0,36 / 0,88 |
| 7618 nửa sau đẩy xuống 160 | 0,45 | 0,43 | 0,43 |

## Kết quả trên dữ liệu thật

Tôi chạy `residual_findings` offline trên tài liệu phụ đề r0 cùng edit spec r1 lưu trong MinIO.

- 7616569292510948709 không có finding. Trước khi sửa, nó báo `x=0 y=1470 w=1061 h=112 at 500, 1000, 2000, 2500 ms`, đúng như flag của g9.
- 7618513827751629177 không có finding.
- Canary trên dữ liệu thật: cover dịch lên 80 và 150 px, và nửa sau các hộp đẩy xuống 160 px, đều bị báo ở cả hai video (6 trên 6). Cover dịch lên 40 px không còn bị báo ở cả hai video (xem phần điểm còn lo).

## Test

Thay đổi nằm trong `services/localization/tests/agents/test_review_checks.py`. Tôi viết test đỏ trước: 2 test fail trên code cũ, gồm regression 7616 và test xen kẽ.

- `test_a_line_the_model_puts_lower_now_and_then_is_not_left_over_subtitle` chạy trên đủ 134 hộp thật của 7616 và cover thật, rồi kiểm không có finding. Fixture đã được đối chiếu và trùng khớp với JSON trong MinIO.
- `test_a_cover_above_the_line_is_reported_on_the_real_boxes[80, 150]` dùng hộp thật của 7616 và cover dịch lên, rồi kiểm có báo.
- `test_a_line_that_moves_below_the_cover_halfway_is_reported_on_the_real_boxes` dùng hộp thật, đẩy nửa sau xuống 160 px, rồi kiểm có báo.
- `test_boxes_outside_between_frames_inside_are_the_models_error`: 3 frame ngoài xen 1 frame trong (75% frame ở ngoài) thì không báo, còn 4 frame ngoài xen 1 frame trong thì báo. Test này khóa độ dài chuỗi ở đúng 2 s.
- Các test cũ vẫn giữ nguyên và đều qua: dưới cover 40 frame, nhiễu 22%, biên 1/3, dải ngắn 3 hay 4 frame, đổi tỉ lệ pixel, không cover.

Kết quả chạy:

- `uv run pytest -q tests/agents/test_review_checks.py`: 37 passed.
- `uv run pytest -q tests/agents tests/activities`: 594 passed, 2 deselected.
- `ruff check` và `ruff format --check` trên 2 file: sạch.

## Điểm còn lo

- Cover hụt khoảng 40 px không còn bị bắt. Ảnh `7616-21000-cover-up-40.png` cho thấy khi đó vẫn lộ một dải chân chữ khoảng 10–20 px. Rule cũ "bắt" được ca này chỉ là trùng hợp: share của nó (0,41) gần bằng share của báo nhầm thật (0,39), nên đó không phải là phát hiện. Với bộ dò lệch 70–90 px trên khoảng 1/3 số frame, hộp của nó không phân biệt được sót 40 px với nhiễu. Muốn bắt ca này thì cần một bộ dò chính xác hơn, hoặc kiểm bằng hình ảnh trên chính render.
- Ca 7618 với cover dịch lên 80 px chỉ vượt ngưỡng sát nút (0,36 so với 0,33). Một video nhiễu hơn có thể làm sót ca này.
- Một dòng thật ở ngoài cover mà mô hình lại đặt vào trong cover ít nhất mỗi 1,5 s sẽ bị bỏ qua. Trên dữ liệu thật, các canary vẫn bị bắt, nhưng bằng chứng mới chỉ có hai video có `frame_boxes`.
- Tài liệu phụ đề ghi đè cùng key `r0`, nên tôi chỉ kiểm được dữ liệu của g9. Hộp của g5 và g6 không còn trong MinIO. Các flag đó (0, 2000, 2500, 4000 ms) có cùng kiểu lệch.
- Controller cần rebuild `worker-agents` để code mới có hiệu lực.
