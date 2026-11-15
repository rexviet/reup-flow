# Ba cổng bỏ qua video và lần chạy thử 8 video mẫu

**Date**: 2026-10-31 13:55 (Asia/Saigon)
**Severity**: Medium
**Component**: services/localization — workflow, agents, observability
**Status**: Ongoing (code xong và đã chạy thật trên 8 mẫu; còn quyết định về commit và chất lượng bản dịch)

## Việc đã làm

Phiên này đi từ ý tưởng "đưa Dubber vào CodeMode của Pydantic AI" tới một pipeline có ba cổng bỏ qua video không đáng dịch.

- **Dubber CodeMode.** Làm spike, đo, rồi cho một sub-agent triển khai trên nhánh `feat/localization-dubber-code-mode`. Model tự viết code Python, gọi tool bằng `gather`, chạy trong sandbox Monty. Chế độ bật qua `DUBBER_MODE=code`; mặc định trong code vẫn là `single`. Đã thử cùng ý tưởng cho các bước chuẩn bị và cho Localizer, đo bằng số liệu thật, và bỏ cả hai vì không có lợi.
- **Trace Langfuse gọn.** Người dùng chê trace cũ rối và không đọc được agent viết code gì. Đã làm lại thành cây trace có prompt, code đã viết và từng lần gọi tool.
- **Lỗi che mặt.** Video không có lời thoại vẫn bị vẽ ô đen che phụ đề. Nguyên nhân: `lay_out` dựng `Cover` dựa vào vùng phụ đề dù không có câu nào cần phụ đề. Giờ chỉ dựng ô che khi có câu cần phụ đề, kèm test.
- **Cổng quảng cáo.** Jev đọc transcript STT, xác suất bán hàng từ 0,8 trở lên thì kết thúc với `skipped_ad`, không ghi lý do. Trạng thái nằm trong `localization.runs.status`.
- **Cổng tiếng nói và cổng luồng video (cổng 0).** `check_voice` chạy trước demucs. Không có luồng video thì `skipped_no_video`. Tiếng nói dưới 5% thì `published_original`. Dùng Silero VAD trên CPU, chạy ở queue riêng `voice_check`. Kiểm không được thì cho video đi tiếp.
- **Chuyển 215 video của kênh vào MinIO**, rồi dừng đợt chạy toàn bộ khi thấy tốn thời gian vô ích.
- **Lần chạy thử 8 mẫu**, mỗi nhóm hai video: không có luồng video, không có voice, quảng cáo, video thường.
- **Cập nhật sơ đồ kiến trúc** theo flow mới và **bảng chi phí 8 video mẫu** (hai artifact, link trong handoff).

## Kết quả 8 mẫu

Cả 8 kết thúc đúng trạng thái kỳ vọng: 2 `skipped_no_video`, 2 `published_original`, 2 `skipped_ad`, 2 `published`.

Chi phí 8 video: LLM ≈ $0,012, GPU 419 giây STT + TTS (≈ $0,047 nếu giá thuê $0,40/giờ, giả định chưa đối chiếu hoá đơn). Bốn video bị loại ở cổng 0 tốn đúng 0 đồng. Hai video quảng cáo vẫn tốn ít OCR và STT vì bị loại sau bước chuẩn bị.

Một trong hai video đi hết pipeline (`7665948504426075877`) có qua CodeMode của Dubber (4 lần gọi model, 3 lần `run_code`). Video kia (`7664547853368951397`) không có câu nào cần Dubber nên không gọi.

## Sự thật khó nghe

- **Chuyển giao sai về Vast.ai.** Handoff trước khuyên "Stop, không Destroy" khi máy rảnh. Lời khuyên đó bỏ sót rủi ro: máy bị Stop không giữ GPU, lần Start sau có thể kẹt ở "Scheduling" hàng giờ tới hàng tuần. Điều này đã xảy ra với máy 4090 sau lần chạy trước. Đã ghi memory và sửa dòng trong handoff cũ.
- **Phải đòi mới chép video ra.** Hai lần liên tiếp mình quên chép kết quả vào `data/review/`: lần đầu chờ cả đợt xong, lần sau chỉ liệt kê đường dẫn gốc của các video bị bỏ qua. Người dùng phải nhắc. Quy tắc "xong cái nào chép cái đó" đã có trong memory và phải áp dụng cho cả video bị bỏ qua.
- **Công cụ chép tự động hỏng im lặng.** Script theo dõi truyền thời điểm bắt đầu thiếu múi giờ, Temporal trả HTTP 400, script chỉ ghi "listing failed" vào log mỗi 15 giây trong khi không chép gì. Mình phát hiện nhờ đọc log, không phải nhờ script báo lỗi rõ.
- **Số liệu chi phí có hai nguồn lệch nhau.** Cột `runs.llm_cost_usd` lệch so với tổng `llm_calls` ở ít nhất một lần chạy ($0,0007 so với $0,0049). Bảng chi phí lấy theo `llm_calls`. Nguyên nhân chưa tìm.
- **Ô trung bình trong bảng chi phí đếm sai** (bỏ sót `published_original`), do người dùng chỉ ra.
- **Chất lượng bản dịch chưa được đánh giá.** Đợt chạy đầu có 5 trong 9 video xong ở `needs_human`: nguồn tiếng Anh, STT nghe sai nặng, Dubber tràn. Hai video dịch của lần thử này mới chỉ chép ra, người dùng chưa xem.

## Bài học

- Một cổng tiết kiệm công sức phải **cho video đi tiếp khi không kiểm được**, vì cái giá của việc loại nhầm cao hơn cái giá của việc xử lý thừa.
- Kết quả "bỏ qua" cũng là kết quả: người dùng cần xem được video bị bỏ qua (bản gốc) để tin cổng đúng. Chép chúng ra `review/` cùng lúc với bản dịch.
- Công cụ phụ chạy nền (watcher, script chép) phải **báo lỗi to**, không được nuốt lỗi vào log.
- Lời khuyên vận hành về dịch vụ thuê phải nói rõ rủi ro mất tài nguyên khi tắt máy.

## Việc tiếp theo

Xem handoff `plans/reports/handoff-261031-1355-cong-bo-qua-va-chay-mau.md`.

**Status**: DONE_WITH_CONCERNS — tính năng chạy đúng trên 8 mẫu, nhưng 132 file còn chưa commit, chất lượng dịch chưa được người dùng đánh giá và quyết định mặc định Dubber chưa chốt.
