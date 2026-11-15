---
type: brainstorm-summary
title: "HyperFrames review, SRT và Localizer có ngữ cảnh"
date: 2026-11-08
status: accepted
source: conversation
next: plan
---

# Thiết kế đã chốt

## Outcome

Pipeline localization giao project HyperFrames để con người mở trong HyperFrames
Studio, review và sửa phụ đề trước khi tự quyết định xuất video. Đầu ra gồm media
nguồn, audio lồng tiếng, SRT và project có các lớp chỉnh sửa được. Workflow không
tự render MP4 cuối hoặc đánh dấu bản localized là đã xuất bản.

Localizer tiếp tục dùng single mode, dịch dựa trên toàn bộ mạch hội thoại. Khi
transcript sai hoặc thiếu, agent đối chiếu câu trước, câu sau và phần lời hiểu được
để phục hồi nghĩa có căn cứ. Không đủ căn cứ thì để trống và đánh dấu cần review.

## Constraints

- SRT giữ nội dung và timing đơn giản; font, size, màu, viền và style lưu trong
  project HyperFrames, không trông chờ SRT giữ style.
- Caption mặc định màu vàng, viền đen; người review có thể chỉnh nội dung, timing,
  xuống dòng, vị trí và style, lưu rồi mở lại project.
- Làm mờ vùng chữ gốc và phủ dải nền đen bán trong suốt kéo hết chiều ngang video.
  Chiều cao theo vùng chữ gốc; các lớp hiệu ứng còn chỉnh được trong project.
  Không dùng yêu cầu cũ về nền ôm sát chiều rộng câu Việt.
- Độ mờ, độ đậm nền và font cụ thể cần kiểm tra bằng hình preview khi triển khai;
  ảnh tham chiếu của người dùng là chuẩn thị giác, chưa chốt con số cấu hình.
- Giữ pipeline chuẩn bị, dịch và lồng tiếng hiện có trong phạm vi bị ảnh hưởng.
  Reviewer tự động còn kiểm tra được dữ liệu trước bàn giao; nhánh bắt buộc render
  video để review phải được thay thế phù hợp với đầu ra project.
- Sửa sub không tự đổi giọng. Nút “Tạo lại giọng câu này” gọi TTS theo nội dung mới
  của đúng câu, thay clip tương ứng, giữ các câu khác và báo khi clip không vừa slot.
  Không âm thầm đẩy lệch timing các câu sau hoặc sửa lại câu người dùng đã viết.
- Localizer nhận toàn bộ transcript trong lượt dịch đầu. Lượt sửa câu phải có ngữ
  cảnh lân cận và ngữ cảnh chung, kể cả khi chỉ yêu cầu sửa một số ID.
- Câu suy luận phải được đánh dấu với lý do trong dữ liệu review; không thêm ghi
  chú đó vào lời thoại SRT hoặc giọng đọc. Không tự thêm sự kiện, tên hay số liệu
  thiếu căn cứ từ nguồn.
- Câu chưa phục hồi được giữ ID và timing trong dữ liệu review, không gọi TTS,
  không tạo cue SRT rỗng. Người dùng có thể điền câu và tạo giọng còn thiếu.
- Ngữ cảnh Localizer ở phạm vi này là transcript; chưa thêm công cụ nghe lại audio
  hay xem video để suy luận. Nhãn ngôn ngữ STT chỉ là gợi ý.
- Khi triển khai, bảo toàn khả năng đọc dữ liệu cũ và replay Temporal; không tự
  chạy lại video cũ. Không ghi đè thay đổi đang có của người dùng trong workspace.

## Non-goals

- Chuyển Localizer sang tools hoặc CodeMode.
- Xây editor video mới trong web hiện tại thay HyperFrames Studio.
- Tự render hoặc tự đăng video sau review.
- Inpainting xóa chữ gốc, thêm pipeline nhận dạng lại audio hay suy luận thị giác.
- Thay đổi toàn bộ provider STT/TTS hoặc thêm nhiều voice theo nhân vật.

## Acceptance criteria

1. Video localized mới kết thúc bằng project có thể mở trong HyperFrames Studio,
   không gọi render cuối và không báo published chỉ vì kiểm tra tự động đã đạt.
2. Preview có chữ vàng viền đen, vùng chữ gốc làm mờ và dải nền bán trong suốt hết
   chiều ngang. Chiều cao không bị ép đủ hai dòng khi không cần.
3. Sửa nội dung, timing và style rồi lưu/mở lại giữ được chỉnh sửa. SRT xuất lại
   phản ánh nội dung và timing đã sửa; style vẫn thuộc project.
4. Tạo lại giọng chỉ cập nhật câu được chọn; có kiểm tra độ dài và không làm lệch
   các câu còn lại. Thất bại TTS không làm mất clip hoặc chỉnh sửa đang có.
5. Câu nhận dạng sai nhưng có căn cứ ngữ cảnh được dịch và đánh dấu suy luận.
   Câu không đủ căn cứ để trống, vẫn xuất hiện trong danh sách cần review.
6. Câu trống không gây validation failure toàn run, không gọi TTS và không hiện
   caption Việt; điền câu rồi bấm tạo giọng có thể hoàn thiện câu đó.
7. Có kiểm chứng ngữ cảnh trong cả lượt dịch đầu và repair, dữ liệu cũ, Temporal
   replay và các hợp đồng liên quan bị thay đổi.

## Trade-offs

- Single mode được chọn: có sẵn toàn transcript, ít vòng gọi model, phù hợp suy luận
  bằng lời thoại. Phụ thuộc vào ngữ cảnh nằm trong dữ liệu đầu vào và giới hạn context
  model; không đủ nếu cần tự thu thập bằng chứng mới từ audio/video.
- Tools mode: phù hợp truy xuất thêm bằng chứng khi có công cụ tương ứng, nhưng
  thêm vòng gọi model; không tự cải thiện nguồn transcript thiếu thông tin.
- CodeMode: có thể tái sử dụng hướng sandbox của Dubber để gom lời gọi công cụ,
  nhưng Localizer hiện chưa có mode này và lợi ích chưa rõ khi dữ liệu đã nạp sẵn.
  Không thay thế năng lực suy luận của model; cần wiring, guard và replay mới.
- Project HyperFrames cộng SRT được chọn thay chỉ giao media và SRT: có review trực
  quan cùng các lớp chỉnh sửa được, đổi lại phải quản lý project và đường mở Studio.
- Để trống khi thiếu căn cứ được chọn thay bắt buộc đoán mọi câu: giữ đúng mức độ
  chắc chắn nhưng người review phải bổ sung một số lời thoại.

Better approaches: none — hướng được chọn giữ đúng yêu cầu người dùng; Localizer
single hiện đã nhận toàn transcript và HyperFrames là editor được người dùng chọn.

## Evidence và lưu ý cho bước lập kế hoạch

- `services/localization/src/localization/agents/localizer_single.py`: prompt đầu
  nhận toàn transcript; repair hiện chỉ có câu lỗi; validation từ chối câu rỗng.
- `services/localization/src/localization/agents/localizer.py`: tools mode đọc
  transcript và span sub gốc, chưa đọc nội dung chữ gốc hay nghe lại audio.
- `services/localization/src/localization/agents/dubber.py`: CodeMode hiện phục vụ
  gọi công cụ TTS, kiểm tra và nhận clip, khác nhu cầu suy luận bằng transcript.
- `services/localization/src/localization/activities/layout_captions.py`: cover
  hiện bị ép rộng tối thiểu 60% frame và cao đủ hai dòng; không giữ quy tắc đó cho
  chiều cao dải nền thiết kế mới.
- `services/localization/src/localization/activities/caption_images.py`: renderer
  hiện vẽ chữ trắng có shadow, chưa phải vàng viền đen.
- `services/localization/src/localization/workflows/video_localization.py` và
  `services/localization/src/localization/activities/publish.py`: cuối luồng hiện
  phụ thuộc render MP4; cần thay hợp đồng đầu ra và trạng thái, không chỉ đổi CSS.
- Ví dụ người dùng: `7689288513854595771-translation-output.json`, có các ID
  5, 10, 16, 25–27 để trống và ID 13 chỉ dịch một phần. File bản dịch không đủ
  xác định chính xác lời nguồn; cần fixture nguồn tương ứng khi kiểm chứng chất lượng.
- `scripts/spike-chunked-multilingual-video.py` có cho phép bản dịch rỗng, khác
  hợp đồng Localizer production hiện tại; không nhầm kết quả spike với pipeline.
- Tài liệu upstream: https://github.com/heygen-com/hyperframes/blob/main/docs/packages/cli.mdx
  và https://github.com/heygen-com/hyperframes/blob/main/docs/guides/captions-and-recuts.mdx.
  Phiên bản CLI, khả năng chỉnh/lưu caption của Studio và cầu nối tạo lại giọng qua
  provider hiện có cần được kiểm chứng khi lập kế hoạch; không coi nút TTS là tính
  năng Studio có sẵn.

Đây là quyết định thiết kế, chưa phải bằng chứng tính năng đã được triển khai.

## Bổ sung đã chốt khi lập kế hoạch

Người dùng xác nhận ngày 2026-11-08:

- Studio và project review chạy trên cùng máy Mac đang chạy dự án; không cần
  phục vụ editor cho thiết bị khác qua LAN/Tailscale trong phạm vi này.
- Dùng bảng review nhỏ đi kèm Studio để sửa câu, timing, lưu SRT và tạo lại giọng;
  xem và chỉnh hình thức trong Studio. Lựa chọn này thay giả định ban đầu rằng mọi
  thao tác sửa/lưu caption đều có sẵn trong Studio. Không fork Studio.
- Giọng mới được tăng tốc tối đa 1,15× để vừa slot. Nếu vẫn quá dài, báo cần sửa
  và giữ audio đang dùng; câu chữ vừa sửa vẫn được giữ và hiện cảnh báo lệch giọng.
  Câu trước đó chưa có audio thì vẫn giữ trạng thái thiếu giọng khi tạo mới thất bại.
- Kế hoạch triển khai: [HyperFrames review và Localizer có ngữ cảnh](../261108-2224-hyperframes-review-contextual-localizer/plan.md).
