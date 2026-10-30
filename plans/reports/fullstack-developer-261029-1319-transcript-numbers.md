# Ghép lại số bị STT tách chữ số, và không cắt segment giữa một con số

Ngày: 2026-10-29. Kế hoạch: `plans/261029-1106-faster-render-and-tts/plan.md`, Đợt 2, mục C.

## Thay đổi

File sửa: `services/localization/src/localization/activities/transcribe.py` và
`services/localization/tests/workflows/test_prep_stage.py` (nơi đã có các test regroup).

- Thêm hằng `NUMBER_GAP_MS = 100`. Trong dữ liệu thật, khoảng hở giữa các word chữ số
  (`3`/`0`, `1`/`0`/`0`, `4`/`.`/`6`) đều là 0 ms, vì provider trả timestamp liền nhau.
  Hở quá 100 ms thì coi là một quãng ngừng thật, và hai bên là hai số riêng.
- `_numbers(words)`: trước khi nhóm theo khoảng lặng, gộp các word chữ số liền nhau
  thành một word, ví dụ `3 0` thành `30` và `1 0 0` thành `100`. Một dấu `.` nằm giữa
  hai cụm chữ số được ghép làm dấu thập phân (`4 . 6` thành `4.6`, tối đa một lần).
  Một dấu `,` chỉ được ghép khi theo sau đúng 3 chữ số (`1 , 0 0 0` thành `1,000`), vì
  tiếng Trung của provider dùng `,` cả làm dấu phẩy ngắt câu (thấy ở video 7618). Mọi
  word khác giữ nguyên, và `_join` không bị đổi.
- Vì số đã thành một word, máy cắt không thể cắt vào giữa nó. Dấu `.` sau `4` cũng
  không còn là "cuối câu" để máy cắt ưu tiên nữa.
- `_holds`: `_best_cut` không cắt giữa một số và word chữ cái nói liền sau nó
  (`30|岁`, `100|个`, `4.6|安`). Nếu số rơi đúng vào giới hạn 60 ký tự thì chỗ cắt dời
  về trước số hoặc ra sau lượng từ.
- `doc.words` vẫn lưu nguyên các word của provider; chỉ segment dùng word đã gộp.
  Không có code nào khác đọc `doc.words` (đã kiểm tra bằng `rg`).
- Nhánh không có word timestamp (`_spread`) không đổi.

## Trước và sau trên transcript thật

Dữ liệu: `work/<id>/r0/transcript.json` lấy từ MinIO qua `worker-media`. File này có
đủ danh sách word của provider: 318 word cho 7616 và 474 cho 7618. "Trước" là các
segment đã lưu, do code cũ tạo ra. "Sau" là kết quả chạy `regroup` mới trên cùng các
word đó. Ở cả hai video, chuỗi word không đổi, và văn bản nối lại (bỏ dấu cách) giống
hệt bản trước.

### 7616569292510948709 (8 segment trước, 7 sau, dài nhất 57)

Trước:
```
0 3 0岁更像2 0岁向上的生命力自己自足的光真的不会暗吃的干净坚持运动规律作息还有一个我从不落下的适当补给
1 特别是每1 0 0个女生7 0个缺铁所以越早重视越好补铁我只看4点铁源含量口感安全性
2 对比无数款我一直回购的就是这个无垢女博士附铁软糖它跟市面上普通铁技真的不一样用的是五重优质有机铁
3 温和好吸收还不刺激还搭配了血蛋白多钛花青素和高浓度维系效率特别高小小一颗就有4 .
4 6安纳铁每天吃三颗刚好满足一天所需像那一吃了半斤枸杞一
5 斤牛肉两斤红枣咱们平时根本吃不了这么多它一颗就能办理搞定酸酸甜甜的荔枝味非常好坚持热量跟一口苹果差不多
6 像我这种常年管理期的人也不用担心女生吃铁真的要趁早
7 现在品牌还有活动以前一瓶的价格现在到手三大罐爱自己从每天三颗附铁软糖开始吧
```
Sau:
```
0 30岁更像20岁向上的生命力自己自足的光真的不会暗吃的干净坚持运动规律作息还有一个我从不落下的适当补给
1 特别是每100个女生70个缺铁所以越早重视越好补铁我只看4点铁源含量口感安全性
2 对比无数款我一直回购的就是这个无垢女博士附铁软糖它跟市面上普通铁技真的不一样用的是五重优质有机铁
3 温和好吸收还不刺激还搭配了血蛋白多钛花青素和高浓度维系效率特别高小小一颗就有4.6安纳铁每天吃三颗刚好满足一天所需
4 像那一吃了半斤枸杞一斤牛肉两斤红枣咱们平时根本吃不了这么多它一颗就能办理搞定酸酸甜甜的荔枝味非常好坚持
5 热量跟一口苹果差不多像我这种常年管理期的人也不用担心女
6 生吃铁真的要趁早现在品牌还有活动以前一瓶的价格现在到手三大罐爱自己从每天三颗附铁软糖开始吧
```

### 7618513827751629177 (11 segment, không đổi)

Video này không có chữ số nào. Các dấu `,` trong đó là dấu phẩy ngắt câu. Cả 11
segment sau khi chạy lại giống hệt bản trước, ví dụ:
```
9  温诺蒂尔核心妆颜配方能打开毛囊通道给硬化的毛囊松绑从根上防脱当时用了一段时间,明显感觉掉发少了,
10 发根也更稳固了,就像练肩背一样,线条和支撑力都在线,身体要长练,头发当然也要好好保养,状态和心情都会慢慢回来的。
```

## Test

- Viết test trước. 7 test mới đỏ trên code cũ; 2 test còn lại là regression và xanh từ
  đầu. Các test dùng đúng hình dạng thật: mỗi chữ Hán một word, timestamp liền nhau,
  mỗi chữ số một word riêng. Các ca được phủ: `3 0岁`, `2 0岁`, `1 0 0个`, `7 0个`,
  `4 . 6安纳`; số `1 , 0 0 0` so với dấu phẩy ngắt câu `3 , 5`; quãng ngừng 300 ms giữa
  `3` và `5` (vẫn là hai số); văn bản không phải số giữ nguyên (`iPhone 15 .`, `A 4纸`);
  câu thật của 7616 trước đây bị cắt sau `4 .`; số nằm đúng ở giới hạn 60 ký tự; số và
  lượng từ không bị tách khi chỗ ngừng dài nhất nằm giữa chúng; `words` lưu vẫn là
  word của provider.
- `uv run pytest -q tests/workflows/test_prep_stage.py tests/activities --ignore=tests/activities/test_tts_pieces.py`:
  271 passed.
- `ruff check` và `ruff format --check` trên hai file đã sửa đều sạch.
- Thử phá từng quy tắc, mỗi lần trên một bản sao rồi khôi phục. Lần nào cũng có test đỏ:
  bỏ `_holds` thì đỏ 2, đặt `NUMBER_GAP_MS` rất lớn thì đỏ 1, nhận mọi dấu phẩy thì
  đỏ 1, bỏ bước gộp số thì đỏ 6.

## Lưu ý

- `tests/activities/test_tts_pieces.py` là file chưa track của agent làm song song,
  import module `localization.activities.tts_pieces` chưa tồn tại, nên tôi bỏ nó khi
  chạy. Tôi không động vào file đó.
- Với 7616, ranh giới của các segment phía sau đã dịch đi. Có một chỗ cắt mới rơi giữa
  từ `女|生` (segment 5/6), trong khi bản cũ cắt giữa `枸杞一|斤`. Cả hai đều do máy cắt
  phải dựa vào thời lượng word khi không có dấu câu; giới hạn này có sẵn từ trước, thay
  đổi này không gây ra nó.
- Dấu `.` giữa hai chữ số luôn được hiểu là dấu thập phân. Một câu kết thúc bằng số
  rồi câu sau bắt đầu ngay bằng số mà không có quãng ngừng sẽ bị ghép nhầm; trường hợp
  này hiếm.
- Chỉ xét chữ số ASCII. Số viết bằng chữ Hán (`三颗`) không bị ảnh hưởng.
- Transcript đã lưu trong MinIO không được tạo lại; chỉ lần chạy `transcribe` sau mới
  dùng code mới.
