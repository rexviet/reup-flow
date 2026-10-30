# Vì sao một video mất 12–13 phút, và cách tăng tốc

Ngày 2026-10-29. Số liệu lấy từ history Temporal của hai run chạy lần lượt, mỗi lúc chỉ một video: `video-7616569292510948709-g3` (67 s) và `video-7618513827751629177-g3` (84 s).

## Kết luận

**Render chiếm khoảng 60% thời gian**, và gần như toàn bộ phần đó có thể bỏ. Remotion mở Chrome và vẽ lại từng khung hình của video. Trong khi đó, nội dung cần dựng chỉ gồm video gốc, một khung che màu cố định và các dòng phụ đề tĩnh. Nếu dựng cùng nội dung bằng ffmpeg trong một lượt, thời gian giảm từ khoảng 217 s xuống 8,9 s. Đo trên chính spec của 7616, lúc máy không chạy việc gì khác.

## Thời gian theo từng bước

| Bước | 7616 | 7618 |
|---|---|---|
| Tổng | 710 s | 786 s |
| Render (2 vòng) | 223 + 203 s | 257 + 225 s* |
| TTS | 117 s (vòng 1) | 69 s (vòng 1), 57 s (vòng 2) |
| Ingest (tách giọng) | 59 s | 59 s |
| STT | 54 s | 16 s |
| LLM (dịch, sửa, review) | khoảng 43 s | khoảng 65 s |

\* Lần render thứ hai của 7618 chạy chồng vài phút với phép đo ffmpeg đầu tiên, phép đo này đã bị dừng giữa chừng. Con số 225 s có thể cao hơn thực tế một chút.

Theo `render.timing` của 7616, các phần của một lần render là: vẽ khung hình (`frames_ms`) 217 s, encode 4,3 s, tải dữ liệu và chọn composition khoảng 1 s. Như vậy encode không phải nút thắt; nút thắt là bước vẽ khung hình.

## Phép đo ffmpeg

Script đo nằm ở scratchpad của phiên, chưa đưa vào repo. Nó dựng trong một lượt: video gốc, `drawbox` cho khung che, 14 lớp `drawtext` cho phụ đề, cộng âm thanh lồng tiếng, và encode bằng libx264 ở 1080x1920 trong container `worker-media`.

| Preset | Encode | Kích thước file |
|---|---|---|
| veryfast | 8,9 s | 21,4 MB |
| medium | 19,3 s | 22,4 MB |

So cùng một khung hình ở giây 18,2 (vùng phụ đề), bản Remotion và bản ffmpeg nhìn gần như giống hệt. Có một khác biệt nhỏ: bóng chữ của `drawtext` không mờ như `text-shadow` của CSS. Nếu cần giống tuyệt đối, có thể dùng Chrome vẽ mỗi dòng phụ đề thành ảnh PNG một lần (14 ảnh), rồi để ffmpeg phủ các ảnh đó lên video.

## TTS

Request gửi lần lượt từng câu, mỗi câu khoảng 6 s. Workflow chia câu thành các batch chạy song song, nhưng batch chia lệch: ở 7618, một batch có 2 câu, batch kia có 10 câu. Vì vậy trên thực tế các câu vẫn chạy gần như nối tiếp nhau. Những lúc hai request chạy cùng lúc, mỗi request vẫn mất khoảng 6–7 s, tức Colab xử lý được song song.

Ở 7616 có một request đầu tiên mất 71 s. Tôi từng nghi Colab nạp lại model khi chuyển từ STT sang TTS, nhưng 7618 không bị như vậy, nên **giả thuyết này chưa được chứng minh**. Nhiều khả năng đây là lượt chạy đầu tiên sau khi Colab vừa khởi động lại.

## Hướng tăng tốc, theo mức tiết kiệm

1. **Thay render Remotion bằng ffmpeg chạy một lượt.** Mỗi lần render giảm từ khoảng 220 s xuống khoảng 10–20 s. Với 7616 là tiết kiệm khoảng 400 s, tổng thời gian còn khoảng 5 phút. Việc này đổi kiến trúc của worker render, gồm image, contract và các test hiện có, nên cần một kế hoạch riêng.
2. **Chia batch TTS đều nhau, hoặc dùng một pool chạy đồng thời 2–3 request.** Theo số đo, TTS ước giảm khoảng một nửa, tức khoảng 30–60 s mỗi vòng.
3. **Ingest (tách giọng, 59 s) và STT** chưa được đo chi tiết, chưa có đề xuất.

## Câu hỏi còn mở

- Phụ đề giữ đúng kiểu chữ CSS (vẽ PNG bằng Chrome rồi phủ) hay chấp nhận `drawtext` / libass?
- Có giữ Remotion làm phương án dự phòng không, hay bỏ hẳn?
