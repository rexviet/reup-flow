# Spike: STT của Vbee có nghe được tiếng Trung không

Ngày: 2026-10-29. Đã gọi API thật với `VBEE_APP_ID` và `VBEE_API_TOKEN` từ `.env`. Không giá trị nào bị in ra hay ghi xuống file. Sau khi chạy, tôi đã kiểm tra mọi file JSON đã lưu và file script: không file nào chứa hai giá trị đó.

## Câu trả lời: chỉ được một phần, và theo một cách không ngờ

Vbee **không phiên âm** tiếng Trung. Với audio tiếng Trung, nó trả về **một bản dịch tiếng Việt trôi chảy**. Trong kết quả không có chữ Hán nào, nhưng cũng không phải tiếng Việt vô nghĩa, không phải lỗi và không rỗng. Bản dịch này đúng nghĩa với transcript whisperx của cùng đoạn audio.

Vì vậy Vbee **không thay được whisperx ở bước transcribe**, vì pipeline cần văn bản nguồn tiếng Trung và timestamp chi tiết. Lý do:

- Response không có trường ngôn ngữ, và tài liệu cũng không có tham số ngôn ngữ.
- Không có văn bản tiếng Trung.
- Cả đoạn 19 giây chỉ ra **một** utterance, kéo dài `0 → 18.93`.

Nếu dùng Vbee thì chỉ là một công cụ dịch-từ-giọng-nói (zh audio → vi text) để tham khảo hoặc đối chiếu. Kết quả không có ranh giới câu, nên không căn được thời gian cho phụ đề hay lồng tiếng.

## Endpoint, xác thực, chế độ gọi

- `POST https://api.vbee.vn/v1/stt`, gửi multipart với `audioContent` (WAV) và `mode`. Hai header `Authorization: Bearer <JWT>` và `App-Id` đều hoạt động.
- **Gói hiện tại không có `sync`.** Gọi `mode=sync` nhận về HTTP 403 `{"error":{"code":"FORBIDDEN","message":"Missing feature: stt-sync"}}`. Đây là lỗi quyền theo gói, không phải lỗi xác thực.
- `mode=async` chạy được, dù không có `webhookUrl`.
  - Bước submit trả `{"transcriptId": "...", "status": "PENDING"}` trong khoảng 0,4–0,5 giây.
  - Sau đó poll `GET https://api.vbee.vn/v1/stt/transcripts/{id}`, đúng URL ghi ở đầu trang tài liệu chứ không phải `/api/v1/...` như trong ví dụ curl. Poll đến khi status là `COMPLETED`.
- Tôi không gặp lỗi nào gợi ý có tham số `language`. Theo đúng kế hoạch, tôi **không** gọi thêm lần nào với `language=zh`.

## Bằng chứng (đã cắt gọn, không chứa secret)

Raw JSON lưu ở `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/07f62d39-b5c7-4084-a633-0d769a53fb9d/scratchpad/vbee/responses/`.

Đối chứng tiếng Việt: 9 giây đầu của `work/7616569292510948709/r1/dubbed.wav`.

```json
{"transcriptId": "0de0ed3d-…", "status": "COMPLETED", "audioDurationSeconds": 10,
 "transcript": "ba mươi tuổi mà trông như hai mươi sức sống từ thân tỏa ra ánh sáng từ trụ thực sự không bao giờ tắt anh uống sạch sẽ kiên trì vận động ngủ nghỉ điều độ còn có một thứ tôi không bao giờ bỏ qua là bổ sung dinh dưỡng",
 "utterances": [{"text": "…", "startTime": 0, "endTime": 9}]}
```

Ở đây Vbee phiên âm đúng: chữ thường, không dấu câu, có lỗi nghe nhỏ kiểu "từ trụ", "anh uống".

Tiếng Trung: 18,93 giây đầu của `work/7616569292510948709/r0/vocals.wav`. Tôi cắt đúng ở ranh giới segment 1/2 của whisperx để so sánh cho sạch.

```json
{"transcriptId": "14b95632-…", "status": "COMPLETED", "audioDurationSeconds": 19,
 "transcript": "Ba mươi tuổi trông như hai mươi tuổi, sức sống hướng thượng, ánh sáng tự túc của mình thật sự không bao giờ tắt. Ăn sạch sẽ, tập thể dục đều đặn, ngủ đúng giờ, và một điều tôi không bao giờ bỏ qua là bổ sung thích hợp. Đặc biệt là cứ một trăm phụ nữ thì bảy mươi người thiếu sắt, nên càng sớm càng tốt. Bổ sung sắt, tôi chỉ xem bốn điểm: nguồn sắt, hàm lượng, cảm giác vị, và tính an toàn.",
 "utterances": [{"text": "<như trên>", "startTime": 0, "endTime": 18.93}]}
```

Kết quả tiếng Trung có dấu câu và viết hoa, khác hẳn kiểu output thô của đối chứng tiếng Việt. Điều này gợi ý Vbee nhận ra audio không phải tiếng Việt rồi đưa qua một bước dịch hoặc LLM. Đây chỉ là suy luận, chưa có tài liệu xác nhận.

## So sánh với whisperx (cùng 0–18,93 giây)

| Whisperx (zh, 2 segment) | Vbee (vi, 1 utterance) |
|---|---|
| `[0.031–10.564]` 3 0岁更像2 0岁向上的生命力自己自足的光真的不会暗吃的干净坚持运动规律作息还有一个我从不落下的适当补给 | Ba mươi tuổi trông như hai mươi tuổi, sức sống hướng thượng, ánh sáng tự túc của mình thật sự không bao giờ tắt. Ăn sạch sẽ, tập thể dục đều đặn, ngủ đúng giờ, và một điều tôi không bao giờ bỏ qua là bổ sung thích hợp. |
| `[10.564–18.933]` 特别是每1 0 0个女生7 0个缺铁所以越早重视越好补铁我只看4点铁源含量口感安全性 | Đặc biệt là cứ một trăm phụ nữ thì bảy mươi người thiếu sắt, nên càng sớm càng tốt. Bổ sung sắt, tôi chỉ xem bốn điểm: nguồn sắt, hàm lượng, cảm giác vị, và tính an toàn. |

Về nội dung, bản dịch giữ đủ các ý:

- Đủ mọi mệnh đề.
- Đủ các con số: 30/20, 100/70, 4 điểm.
- Đủ bốn tiêu chí: nguồn sắt, hàm lượng, khẩu vị, an toàn.

Có vài chỗ dịch sát chữ: "自己自足的光" thành "ánh sáng tự túc", "口感" thành "cảm giác vị". Về thời gian, whisperx có 2 segment và 318 timestamp mức từ cho cả video, còn Vbee chỉ có 1 khối duy nhất.

## Timestamp

Timestamp chỉ ở mức utterance, gồm `startTime`, `endTime` (giây) và `text`. Không có mức từ hay ký tự, không có confidence. Trong cả hai lần gọi, toàn bộ clip ra **một** utterance duy nhất: `0–9` và `0–18.93`. Với clip ngắn, timestamp này thực tế vô dụng.

## Độ trễ

Độ trễ đo từ lúc submit đến khi có kết quả cuối, poll 3 giây một lần:

| Clip | Submit | Tổng | Số lần poll |
|---|---|---|---|
| vi 9 s | 0,41 s | 33,5 s | 10 |
| zh 18,93 s | 0,48 s | 55,4 s | 18 |

Như vậy độ trễ vào khoảng 3 lần thời lượng audio, chậm hơn nhiều so với "~500 ms" mà tài liệu ghi cho realtime. Đây chỉ là 2 mẫu, chưa đủ để tính p50.

## Số lần gọi

- 3 POST:
  - 1 lần `sync` bị từ chối 403, không tính phí.
  - 2 job `async`, bị tính phí `audioDurationSeconds` = 10 và 19.
- 28 GET poll.
- Không gọi `language=zh`, vì không có tín hiệu nào cho thấy tham số này tồn tại.

## Cách chạy lại

1. Lấy audio vào scratchpad. Chạy lệnh dưới đây lần lượt với từng key `r0/vocals.wav`, `r0/transcript.json`, `r1/dubbed.wav`:
   ```bash
   docker compose exec -T worker-media python -c "from localization.config import Settings; from localization.storage.object_store import ObjectStore; open('/tmp/x','wb').write(ObjectStore.from_settings(Settings()).get_bytes('work/7616569292510948709/r0/vocals.wav', produced_by='ingest'))"
   docker exec $(docker compose ps -q worker-media) cat /tmp/x > "$S/r0_vocals.wav"
   ```
2. Cắt và chuyển sang WAV mono 16 kHz PCM16:
   ```bash
   ffmpeg -y -i "$S/r0_vocals.wav" -t 18.93 -ac 1 -ar 16000 -c:a pcm_s16le "$S/zh_19s.wav"
   ffmpeg -y -i "$S/r1_dubbed.wav" -t 9 -ac 1 -ar 16000 -c:a pcm_s16le "$S/vi_9s.wav"
   ```
3. Gọi Vbee từ `services/localization`. Cần có `VBEE_APP_ID` và `VBEE_API_TOKEN` trong `.env` ở gốc repo.
   ```bash
   uv run python eval/vbee_stt_spike.py "$S/zh_19s.wav" --mode async --out "$S/responses"
   # tuỳ chọn: --field language=zh, --timeout 300
   ```
   Script lưu JSON của bước submit và kết quả cuối, rồi in tóm tắt gồm status, ngôn ngữ (nếu có), số segment, mức timestamp, độ trễ và text.

## Câu hỏi còn mở

1. Vbee có cố ý dịch mọi audio không phải tiếng Việt sang tiếng Việt không, và có cách nào tắt để lấy bản phiên âm gốc không? Tài liệu không nói gì về việc này. Cần hỏi Vbee.
2. Với audio dài hơn, ví dụ cả video 67 giây, Vbee có tách thành nhiều utterance không, hay vẫn trả một khối? Spike này chưa thử, để giữ chi phí nhỏ.
3. Gói nào có `stt-sync`?
