---
phase: 4
title: Một rendition thật, làm bằng tay (M2)
status: not_started
effort: 0.5 ngày
blockedBy: [3]
gate: "GATE B — go/no-go chất lượng dub"
---

# Phase 4 — Một rendition, làm bằng tay

**Không viết lane nào ở phase này.** Một script tay, một video, một lần nghe.

---

## Vì sao phase này tồn tại riêng

Solution design §13 nói thẳng: **"Đừng nhập M2 vào M3. Nếu voice clone tiếng Việt nghe tệ, cần biết ở ngày thứ 3 chứ không phải tuần thứ 6."**

Phase 0 spike S1 đã nghe thử với **một clip bất kỳ**. Phase này nghe thử với **một clip thật vừa tải qua Module 1, của một talent thật, với giọng clone của chính talent đó**. Khác nhau ở ba chỗ:
- Clip thật có nhạc nền Douyin thật, không phải audio sạch phòng thu
- Caption thật có hashtag `#话题`, `@mention`, emoji lẫn lộn
- Giọng clone từ audio thật của talent, không phải mẫu chọn kỹ

---

## Làm

1. Chọn 1 video từ `videos` sau Phase 3, ưu tiên video **có nhạc nền rõ** và **có tên riêng trong lời nói**.
2. Clone giọng talent đó qua OmniVoice, ghi `voice_id` vào `talent_voices` bằng tay.
3. Ghi `voice_consent_at` cho talent — **kể cả ở phase làm tay**. Nếu talent này chưa có consent thì chọn talent khác. Đây là tiền lệ, và tiền lệ ở phase PoC là thứ sẽ được copy vào production.
4. Gọi bằng tay:
   ```
   POST /batch/enqueue   video=<file thật>  langs=vi  voice_id=<clone>  preserve_bg=true
   ```
5. Poll `GET /batch/jobs/{id}` → `GET /batch/download/{id}/vi`.
6. Dịch caption bằng tay qua `POST /dub/translate`, một segment, `provider=openai`, `quality=cinematic`, `glossary` = tên talent + tên sản phẩm.
7. Conform 9:16 bằng ffmpeg tay.
8. **Nghe. Xem. Đọc caption.**

---

## Ghi lại — bốn con số quyết định Phase 5

| Đo | Vì sao quan trọng |
|---|---|
| Nhạc nền còn không, mix có bị đè giọng không | `preserve_bg=true` là lý do chọn D2-A1 thay vì tự làm ducking |
| Giọng có drift giữa câu không | Mặc định `voice_match="per_line"`. Nếu drift lộ rõ → Phase 5 đi đường granular ngay, không chờ `reject_reason` |
| `timing_strategy="concise"` cắt mất bao nhiêu nghĩa | Nếu cắt nặng → cùng kết luận như trên |
| Tên riêng trong **thoại** bị dịch máy thành gì | Batch không nhận `glossary`. Caption có. Nếu thoại sai tên riêng nhiều → lý do thứ ba để leo lên D2-A2 |

---

## Gate B — ba nhánh, không phải hai

| Kết quả nghe | Phase 5 làm gì |
|---|---|
| **Nghe được** | Đi D2-A1: 3 lane, `POST /batch/enqueue`. Đúng như plan |
| **Tạm được nhưng drift/cắt nghĩa rõ** | Đi **D2-A2 ngay từ đầu**: 5 lane, `voice_match: "consistent"` + `timing_strategy: "smart_fit"`. Effort Phase 5 tăng ~2 ngày. **Rẻ hơn nhiều so với viết A1 rồi viết lại** |
| **Tệ** | Thử engine khác trong 14 engine ở Settings. Hết engine mà vẫn tệ → **dự án dừng**, không có ngôn ngữ đích thứ hai để lùi về |

> Nhánh giữa là lý do phase này không gộp được. Nó chỉ trả lời được bằng tai người trên dữ liệu thật, và nó **đổi kiến trúc Phase 5** — 3 lane hay 5 lane.

---

## Definition of done

- [ ] Một file mp4 tiếng Việt, nghe được, có nhạc nền gốc, ở `/data/output/`
- [ ] Một caption tiếng Việt, đọc được, tên riêng đúng
- [ ] Bốn con số ở bảng trên đã ghi vào `docs/reports/spike-results-2026-08-24.md`
- [ ] **Quyết định D2-A1 hay D2-A2 đã chốt bằng văn bản** trước khi Phase 5 bắt đầu
