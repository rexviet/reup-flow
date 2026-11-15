# Triển khai review project

Review delivery dừng ở bàn giao cho người review, không tự xuất bản. [Companion](review-companion.md)
sở hữu hướng dẫn sửa phụ đề/style/revoice; [compatibility spike](hyperframes-review-spike.md)
sở hữu gate native Studio. Người dùng đã yêu cầu bật review delivery trên Mac hiện tại;
[Compose override](../docker-compose.override.yml) giữ lựa chọn này qua restart.
Các acceptance native round-trip còn lại vẫn được ghi riêng, chưa coi là đã pass.
Launcher có boundary preload và đã mở/phát full canary với timeline 03:09;
[bằng chứng](../plans/reports/ops-261110-1916-native-studio-full-video.md). Không bỏ probe.

## Điều kiện trước triển khai

Review-project Localizer mới có activity `localizer_source_evidence`: đọc chữ gốc
trên nhiều frame theo timestamp từ source snapshot của đúng generation, rồi đưa
bằng chứng vào cả prompt đầu và prompt sửa. OCR đọc nguyên văn, không dịch; Localizer
đối chiếu với ASR và ngữ cảnh. Evidence không đủ vẫn phải `unresolved`, không bịa
câu để lấp chỗ trống. Cache có namespace generation/model/prompt, và marker Temporal
giữ đường replay cũ. Cần deploy worker hiểu activity mới trước khi chạy canary;
legacy delivery không gọi bước này. Source của behavior nằm ở
[`source_evidence.py`](../services/localization/src/localization/activities/source_evidence.py).

VoiceStudio STT chia audio theo Silero VAD trước khi nhận dạng từng chunk. Ngưỡng
speech hiện là 0.25 thay cho mặc định 0.5 để giữ lời thoại ngắn/biểu cảm trên stem
đã tách giọng. Đây là ngưỡng chọn audio, không phải xác nhận lời nói đã hiểu đúng;
Localizer vẫn cần source evidence và giữ `inferred`/`unresolved` khi chưa đủ rõ.

`TTS_TIMEOUT_SECONDS` mặc định 1200 giây, tách khỏi timeout provider 120 giây của
STT/OCR. VoiceStudio có queue và budget inference riêng; client cắt ở 120 giây
có thể tạo retry trong khi request trước chưa kết thúc. Activity synthesis vẫn
heartbeat và có budget 30 phút; HTTP 4xx/5xx giữ phân loại cũ. Giá trị TTS bị chặn
trên 1500 giây để còn thời gian xử lý/cancel trong activity, không phải chờ vô hạn.

Cần chứng minh native Studio bind loopback, reject exact Host/Origin không hợp lệ,
lưu style/SRT và đóng/mở lại không mất thay đổi; sau đó chạy dedicated fixture trong
[kiểm tra live companion](review-companion.md#kiểm-tra). Fixture phải chứng minh câu
inferred/unresolved và all-blank, revoice một cue bằng provider thật, cold-open HEAD,
khôi phục khi mất response và hai generation không trộn asset. Test dùng stub hoặc
skip không thay thế gate này. Không tự chạy lại video cũ để tạo dữ liệu nghiệm thu.

[Compose](../docker-compose.yml) là nguồn cấu hình service. Bridge cần identity riêng
trong nguồn secret `.env` hiện có, được cấp qua [MinIO init](../infra/minio/init.sh).
Không dùng root/worker key. Kiểm tra quyền bằng chính identity triển khai: Get/Put
và scoped List trong `review/`; Delete và source/work/cache/output phải bị từ chối.
[Test policy](../services/localization/tests/integration/test_review_policy.py) chứng minh
policy trên MinIO cô lập, không xác nhận identity đang triển khai đã được provision.
Bridge liveness không chứng minh backend, quyền, session hoặc Studio đã đạt nghiệm thu.

## Thứ tự upgrade

1. Giữ starter ở `legacy_render`. Build binary mới, có cả hai branch và revoice,
   trước khi bật mode mới. Giữ worker tương thích để xử lý histories đã ghi mode.
2. Chạy [migration runner](../scripts/apply-migrations.sh). Runner tạo backup vào ignored
   `backups/`, ghi SHA-256 và thử restore vào clone trước khi áp schema. Mỗi migration
   và ledger insert cùng transaction. Database có bảng nhưng mất ledger chỉ được adopt
   khi khớp [schema evidence](../scripts/migration-evidence.sql); schema lạ phải reconcile,
   không seed mọi migration. Backup chứa dữ liệu riêng tư, không commit hoặc chia sẻ công khai.
3. Deploy worker mới và starter legacy. Provision identity bridge từ nguồn secret hiện có,
   chuẩn bị `data/review`, rồi bật profile review. Chỉ host launcher khởi chạy Studio;
   web và downloader không điều khiển Docker hay ghi Temporal.

```sh
scripts/apply-migrations.sh
docker compose build localization-starter worker-agents worker-media review-bridge
docker compose up -d --no-deps worker-agents worker-media
LOCALIZATION_DELIVERY_MODE=legacy_render docker compose up -d --no-deps --force-recreate localization-starter
# Sau khi đã cấu hình identity riêng và kiểm tra quyền:
docker compose run --rm --no-deps minio-init
mkdir -p data/review
docker compose --profile review up -d --no-deps review-bridge
```

Migration chỉ thêm nullable `project_key`. Không backfill status hoặc trỏ `review_key`
MP4 cũ sang project. API review cần schema mới trước khi deploy web/API mới; khi thiếu
migration hoặc DB lỗi, discovery trả 503 thay vì danh sách rỗng giả.

## Canary và activation

Chọn video đã tải và generation **mới hơn mọi generation đã ghi**; không sửa outbox.
Starter dùng cùng kiểm tra voice/source/input như dispatch thường. Lệnh dưới chỉ start
một input `review_project`, không flip cấu hình cho backlog. Duplicate workflow ID bị từ chối.

```sh
docker compose exec -T localization-starter python -m localization.starter.outbox_starter canary <aweme> <generation-moi>
scripts/e2e-smoke.sh --run <aweme> <generation-moi>
node scripts/review-studio.mjs open <aweme> <generation-moi>
```

Nghiệm thu canary bằng fixture đã duyệt: HEAD và mọi dependency checksum hợp lệ,
đúng generation; không final render/output write hoặc auto publish; sửa/lưu/mở lại,
SRT, style và revoice thật đạt contract. `needs_human` vẫn cần người sửa, không phải
đã xuất bản. `output_key` phải rỗng; với video đã có output từ legacy, kiểm tra object
cũ không bị thay đổi thay vì xóa nó để làm test pass.

Với deployment mới, sau khi đủ bằng chứng hãy lưu `LOCALIZATION_DELIVERY_MODE=review_project` vào
cấu hình starter và recreate **riêng starter**:

```sh
LOCALIZATION_DELIVERY_MODE=review_project docker compose up -d --no-deps --force-recreate localization-starter
```

Giá trị shell trên áp dụng cho lần recreate đó; nguồn cấu hình lâu dài phải giữ cùng
giá trị để lần deploy tiếp không đổi mode ngoài ý muốn. Run đã start giữ input cũ;
rollback default không đổi histories. Vbee tiếp tục legacy.

## Discovery và smoke

Trang dashboard (`services/web`) liệt kê video chờ duyệt qua `GET /api/dashboard/videos`
(lọc chip "Chờ duyệt") và nút **Mở Studio** trong trang chi tiết video; route `/api/reviews`
cũ đã bị gỡ. Phần liệt kê chỉ đọc DB và Temporal; link media do `POST /api/dashboard/media-links` ký. Principal DB vẫn là
role hiện có, chưa được giảm quyền. Dashboard không phải editor; Studio mở trên
máy chạy dashboard qua worker review (yêu cầu mở không mang key/grant).

[Smoke script](../scripts/e2e-smoke.sh) mặc định đòi `ready_for_review`, project hợp lệ
và `output_key` rỗng. Nó materialize bundle bằng worker để kiểm checksum; `needs_human`
có bundle hợp lệ thoát **4**. Các terminal khác/wrong mode/bundle lỗi thoát **1**,
lỗi input/setup thoát **2**, timeout thoát **3**. `--run` kiểm đúng generation canary
mà không enqueue. Cách cũ đòi `published` phải chọn mode explicit:

```sh
scripts/e2e-smoke.sh --mode legacy_render 'https://www.douyin.com/video/<id>'
```

## Rollback

Đặt cấu hình starter về `legacy_render`, recreate riêng starter, rồi ngừng nhận
request bridge mới. Giữ worker binary mới cho project histories và revoice jobs đã
start. Dừng Studio bằng managed launcher; không kill theo pattern hoặc đổi port.

```sh
LOCALIZATION_DELIVERY_MODE=legacy_render docker compose up -d --no-deps --force-recreate localization-starter
docker compose --profile review stop review-bridge
node scripts/review-studio.mjs stop
```

Có thể revert web/API khi schema additive vẫn giữ nguyên. Phục hồi HEAD phải dùng
CAS với ETag hiện tại rồi materialize cache sạch; sửa file local không rollback
HEAD. Giữ snapshot/history/candidate để điều tra. Chỉ down/restore DB sau backup,
khi không còn consumer mới và đã có quyết định vận hành; không rollback worker
về binary không hiểu branch mới.
