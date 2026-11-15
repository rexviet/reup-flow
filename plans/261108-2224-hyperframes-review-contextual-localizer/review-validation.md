# Rà soát và validation kế hoạch

## Trạng thái

Ngày tiếp tục: 2026-11-09, Asia/Saigon. Kế hoạch có 5 phase, tier Full.
Ba reviewer từng bị ngắt bởi usage limit; lượt tiếp tục đã hoàn tất đủ ba báo cáo.
Không tính lượt lỗi ban đầu là review hoàn tất. Không sửa code sản phẩm.

## Validation sản phẩm — 3 câu đã trả lời

1. **Bạn sẽ mở HyperFrames Studio trên cùng máy Mac đang chạy dự án, hay từ thiết bị khác? Điều này quyết định cách mở Studio và truy cập media.**
   - Lựa chọn: cùng máy Mac đang chạy dự án (đề xuất) / từ máy khác qua LAN/Tailscale.
   - Trả lời: “Cùng máy Mac đang chạy dự án (đề xuất)”.
   - Ảnh hưởng: Phase 1, 4, 5 dùng Studio host-local và bridge chỉ publish loopback.
2. **Khi “Tạo lại giọng câu này” tạo ra audio dài hơn khoảng trống của câu, bạn muốn xử lý thế nào?**
   - Lựa chọn: tăng tốc trong giới hạn 1,15×, vẫn dài thì giữ audio cũ (đề xuất) / không tăng tốc, quá dài thì giữ audio cũ.
   - Trả lời: “Cho tăng tốc trong giới hạn hiện có 1,15×; vẫn quá dài thì báo để sửa, giữ bản audio đang dùng (đề xuất)”.
   - Ảnh hưởng: Phase 4 thực sự áp dụng atempo, giữ clip cũ khi lỗi/tràn và giữ câu vừa sửa.
3. **Bạn muốn bố trí phần review thế nào, khi Studio nguyên bản chưa lưu đầy đủ text/timing và chưa có nút tạo lại giọng?**
   - Lựa chọn: bảng review nhỏ cạnh Studio (đề xuất) / patch Studio để mọi thao tác ở cùng chỗ.
   - Trả lời: “Dùng bảng review nhỏ đi kèm Studio: sửa câu/timing, lưu SRT và tạo giọng ở bảng; xem/chỉnh hình thức trong Studio (đề xuất, ít bảo trì hơn)”.
   - Ảnh hưởng: Phase 1, 4 dùng companion; không fork Studio, không suy diễn native caption persistence.

## Các điều chỉnh đã xác minh trước review

- Sửa inventory `review_checks.py` và test tương ứng về thư mục `agents/` thực tế.
- Phân biệt bind HTTP bên trong container (`0.0.0.0`) với publish trên host (`127.0.0.1`).
- Schema exporter hiện chỉ có EditSpec (`services/localization/src/localization/contracts/export.py:13`);
  Phase 3 phải thêm schema/check riêng cho manifest mà không đổi hash EditSpec cũ.

## Điều chỉnh controller đã áp dụng

| Mục | Bằng chứng | Điều chỉnh cần ghi vào kế hoạch |
|---|---|---|
| Bật/tắt rollout chưa có chủ sở hữu cụ thể | `services/localization/src/localization/contracts/models.py:20`; `services/localization/src/localization/starter/outbox_starter.py:240` | Ghi delivery mode trong input, starter inject theo config; legacy default cho input cũ; patch guard và rollback không đọc env khi replay. |
| Migration cũ không có ledger có thể bỏ qua migration mới | `scripts/apply-migrations.sh:30` | Reconcile ledger theo bằng chứng schema từng migration; không seed migration mới chỉ vì bảng videos đã tồn tại; kiểm tra cả fresh, ledger và legacy-no-ledger. |
| Bridge trong container không thể tự mở Studio host | `README.md:183`; `docker-compose.yml:490` | Host launcher riêng, argument arrays, pin CLI, PID/port và one-time session bootstrap; không mount Docker socket vào bridge. |
| Chỉ đổi cue clip có thể không đổi audio đang phát | `services/localization/src/localization/activities/assemble_audio.py:214` | Quy định project dùng đúng một mixed audio active; revoice rebuild derived mix từ clip cũ + clip mới, không TTS lại câu khác, không phát đồng thời mix và per-cue audio. |
| Caption vừa sửa nhưng giọng cũ vẫn cần xuất SRT đúng nội dung mới | `services/localization/src/localization/activities/layout_captions.py:393` | Preview/SRT dùng bản caption đã lưu mới nhất; mapping audio active giữ spoken text/timing riêng và hiển thị mismatch đến khi revoice thành công. |

Các mục trên làm rõ kế hoạch thực thi, đã áp dụng cùng đợt user duyệt review; không đổi ba lựa chọn sản phẩm.

## Red Team Review — 2026-11-09

Ba lượt review tiếp tục đã hoàn tất:

- [Security: 15 fact checks](../reports/review-security-261108-2224-hyperframes-plan.md).
- [Failure: 18 fact/flow checks](../reports/review-failure-261108-2224-hyperframes-plan.md).
- [Assumptions/contracts: 27 checks](../reports/review-assumptions-261108-2224-hyperframes-plan.md).

Sau gộp findings trùng: **7 mục — 2 Critical, 4 High, 1 Medium**.
Các findings có source `file:line`; không có mục bị loại vì thiếu bằng chứng.

| # | Finding | Severity | Adjudication đã áp dụng | Phase |
|---|---|---|---|---|
| 1 | Ledger bootstrap bỏ qua migration mới | Critical | Accept | 5 |
| 2 | Work artifact nguồn collision giữa generation | Critical | Accept | 3 |
| 3 | Thiếu delivery selector và status consumers | High | Accept, gộp failure/assumptions | 2, 3, 5 |
| 4 | Revision chưa write-once và thiếu durable HEAD | High | Accept, gộp failure/security | 3, 4 |
| 5 | Thiếu host Studio launcher; bind container/host lẫn nhau | High | Accept; bind và host launcher đã ghi vào phase | 1, 4, 5 |
| 6 | Bridge nhận credential quá quyền | High | Accept; chỉ cần review storage + Temporal, không provider/DB secret | 4, 5 |
| 7 | Thiếu output encoding/CSP cho caption/reason | Medium | Accept | 1, 3, 4 |

User trả lời ngày 2026-11-09: **“Áp dụng các sửa đổi được đề xuất vào kế hoạch”**.
Cả 7 finding đã Accept và đưa vào phase sở hữu; rejected: 0. Không còn câu hỏi duyệt review.
Hai đề xuất controller về derived audio mix và caption/SRT đã sửa sẽ làm rõ yêu cầu
revoice/save có sẵn, không mở rộng tính năng.

### Whole-Plan Consistency Sweep

Đã cập nhật và đọc lại index cùng cả 5 phase. Decision deltas:

1. Input delivery_mode default legacy + starter toggle; marker chỉ bảo vệ replay.
2. Source snapshot và work producers/consumers có generation từ đầu.
3. project_key là stable HEAD key; media/manifest create-only; HEAD CAS ETag.
4. Local pointer là cache; cold-open/retry phục hồi từ HEAD và Temporal result.
5. Host launcher quản lý Studio; bridge container bind khác host publish; grant TTL/session bootstrap cụ thể.
6. Bridge có principal review-only, không provider/DB/LOC_S3/root secrets.
7. Migration schema-evidence + atomic ledger, test cả legacy-no-ledger.
8. Caption preview/SRT giữ câu mới, audio active giữ spoken identity riêng; revoice rebuild một mixed track.
9. Status enums/labels/smoke/API/barrel/schema consumers được liệt kê đầy đủ.
10. Cue/reason literal encoding + CSP; live gates tách khỏi unit tests và raqm prerequisite.

Không đổi companion UX, giới hạn 1,15×, single mode hoặc quyết định để trống. Technical gate ở Phase 1 vẫn phải pass trước thay production behavior; không khẳng định tính năng đã có.

## Verification Results

**Tier Full.** 60 claim checks của reviewer và 15 check bổ sung của controller đã
đối chiếu source. Đây là kiểm tra hành vi/code hiện có, không phải 75 test tính năng mới.

| # | Claim controller đã xác minh | Source |
|---|---|---|
| 1 | Work key không có generation | `storage/object_store.py:23` |
| 2 | Legacy render key không có generation | `storage/object_store.py:34` |
| 3 | Script writer dùng work key và giữ version ở tên file | `activities/script_document.py:26` |
| 4 | Assembler từ chối text dịch rỗng | `activities/assemble_script.py:65` |
| 5 | Input chưa có delivery selector | `contracts/models.py:20` |
| 6 | Starter tạo input từ settings/row | `starter/outbox_starter.py:240` |
| 7 | Migration thiếu ledger seed tất cả filename | `scripts/apply-migrations.sh:30` |
| 8 | DDL và insert ledger hiện là hai lệnh riêng | `scripts/apply-migrations.sh:50` |
| 9 | Schema exporter chỉ export EditSpec | `contracts/export.py:13` |
| 10 | API-key guard được đăng ký global | `services/downloader/src/app.module.ts:59` |
| 11 | Gate outcomes chưa có ready_for_review | `services/downloader/src/workspace/workspace.schemas.ts:15` |
| 12 | RunProgress chưa có nhãn ready_for_review | `services/web/src/workspace/RunProgress.tsx:45` |
| 13 | Localization image hiện là Python, không có Studio host launcher | `services/localization/Dockerfile:24` |
| 14 | Mixer dựng track từ BGM và từng clip | `activities/assemble_audio.py:214` |
| 15 | Layout legacy đòi script và giọng đồng nhất | `activities/layout_captions.py:393` |

Source ngắn ở bảng trên tương đối với `services/localization/src/localization/`,
trừ các đường dẫn bắt đầu `services/` hoặc `scripts/` tương đối với repository root.

Kiểm tra tài liệu đã chạy: `ak plan validate` hợp lệ, `ak plan parse` nhận đủ
5 phase, 29 liên kết local được kiểm tra và không có link hỏng; `git diff --check`
sạch. Kết quả kiểm tra sau sửa được ghi bổ sung bên dưới. Nguồn sự thật task vẫn là các checkbox
trong phase files; không có runtime task-management surface phù hợp được cung cấp.

### Các kiểm chứng chưa thực hiện

Studio pin/browser round-trip, backend CAS MinIO, provider thật, migration clone,
canary và feature tests là cổng kiểm chứng khi triển khai, chưa được tuyên bố pass.
Baseline scout ghi 131 tests pass và 35 layout tests fail do thiếu Pillow raqm;
không bỏ/giảm assertions để biến prerequisite thiếu thành kết quả xanh.


## Handoff

Kế hoạch đã qua kiểm tra tài liệu cuối và đủ chi tiết để bắt đầu triển khai ở Phase 1;
không có unresolved product decision. Tất cả feature checkboxes vẫn unchecked.
Không coi source review hoặc plan-format validation là thực thi live technical gates.


## Kết quả sau áp dụng — 2026-11-09

- Cả 7 findings accepted đã đưa vào phase sở hữu; rejected 0, unresolved contradictions 0.
- Đã đọc lại index + 5 phase + log; 10 decision deltas được đối chiếu toàn plan. Index 74 dòng, 5 phase phụ thuộc tuần tự không cycle, 19 acceptance checkboxes vẫn chưa hoàn thành.
- Reviewer Failure Mode Analyst [sweep sau sửa](../reports/review-failure-261108-2224-hyperframes-plan.md) xác nhận đóng cả 7 delta, không còn blocker cụ thể. Bổ sung controller: worker io activity là bên commit HEAD cho revoice; bridge poll chỉ đọc outcome, không commit lần hai.
- `ak plan validate`: valid; 23 local links sau sửa không hỏng; 70 file paths được ghi Modify tồn tại. Các đường Create là công việc tương lai, không coi là đã có.
- `ak plan use` đã pin plan hiện tại; `ak plan reindex --apply` đồng bộ index. Không có task-management surface phù hợp, dùng trạng thái phase/checkbox làm authority.
- `git diff --check` sạch. Chỉ tài liệu kế hoạch/báo cáo/nhật ký được viết, không thay code sản phẩm, DB, service hoặc secret.
- Handoff ak-cook giữ nguyên toàn scope; bước đầu là technical proof ở Phase 1. Chưa chạy Studio/provider/backend/migration gates và chưa commit/push.
