# Nghiên cứu HyperFrames cho contextual localizer

Ngày kiểm tra: 2026-11-08 (Asia/Ho_Chi_Minh). Phạm vi là nghiên cứu, không sửa code, không chạy Studio/server, không gọi provider và không đọc credential.

## Kết luận điều hành

Đường nhỏ nhất đáp ứng đầy đủ thiết kế là **giữ HyperFrames Studio làm nơi xem/chỉnh trực quan, nhưng dùng companion review UI/loopback bridge trên cùng Mac làm nơi lưu canonical caption edits và gọi VoiceStudio hiện có**. Không nên đặt nút `Tạo lại giọng câu này` vào Studio chính thức ở phase đầu: upstream không công bố extension/panel API và caption text/timing hiện không được Studio serialize. Nếu nút bắt buộc phải xuất hiện trong Studio, đó là **custom fork/patch của `@hyperframes/studio`**, phải pin và tự bảo trì.

Giữ pin hiện tại `hyperframes@0.8.77` trong kế hoạch để tái lập. Npm hiện đã có `hyperframes@0.9.23` (2026-11-08), nhưng chưa có bằng chứng rằng bản mới đã giải quyết các lỗ hổng round-trip; source map của 0.8.77 cho thấy caption sync vẫn cùng mô hình giới hạn. Pin đã kiểm chứng:

```text
hyperframes@0.8.77
gitHead dd88b5f5b78f25a82ad0677c00bbbfa85428454f
Node >=22
```

## Bằng chứng đã kiểm tra

### Upstream/docs chính thức

- CLI: [docs/packages/cli.mdx](https://github.com/heygen-com/hyperframes/blob/main/docs/packages/cli.mdx) (tài liệu `main`, không phải bản pin). `preview` chạy Studio local, mặc định port 3002, có lifecycle/status URL; thay đổi project file được refresh. `transcribe` nhận SRT/VTT/JSON; `--preserve-cues` được docs `main` mô tả để giữ ranh giới cue, nhưng **chưa được xác nhận có trong 0.8.77**; phase 1 phải kiểm tra bằng CLI pin, không dùng docs `main` như bằng chứng tương thích.
- Studio package: [docs/packages/studio.mdx](https://github.com/heygen-com/hyperframes/blob/main/docs/packages/studio.mdx). `StudioApp` là toàn editor; các component thấp hơn cần state/context/callbacks, không phải drop-in editor. Studio server ghi thay đổi vào project files và reload khi file ngoài thay đổi.
- Caption guide: [docs/guides/captions-and-recuts.mdx](https://github.com/heygen-com/hyperframes/blob/main/docs/guides/captions-and-recuts.mdx). Caption là overlay; review text/timing có thể làm bằng editor/agent, nhưng tài liệu hướng durable transcript replacement qua source/transcribe, không hứa Studio caption edit sẽ ghi transcript source.
- Desktop/local projects: [docs/studio-app/index.mdx](https://github.com/heygen-com/hyperframes/blob/main/docs/studio-app/index.mdx). Project local có thể mở trong browser editor/CLI, sử dụng folder project và `.hyperframes-studio` metadata.
- Caption authoring: [authoring.md](https://github.com/heygen-com/hyperframes/blob/main/skills/media-use/audio/references/captions/authoring.md). Overlay có nền trong suốt; contrast layer nên là `div` tối bán trong suốt ở dưới caption trong composition.
- WebMCP: [docs/weekly-updates.mdx](https://github.com/heygen-com/hyperframes/blob/main/docs/weekly-updates.mdx), [docs/guides/plugins.mdx](https://github.com/heygen-com/hyperframes/blob/main/docs/guides/plugins.mdx). WebMCP hiện phục vụ agent/browser tools; “plugin” trong docs là agent skill, không phải UI extension/panel registry.
- Npm registry: [hyperframes](https://www.npmjs.com/package/hyperframes). Registry tại thời điểm kiểm tra trả 0.9.23; tarball 0.8.77 có `gitHead` nêu trên. Đây là lý do cần giữ pin thay vì dùng `latest`.

### Source/package inspection (đã kiểm tra tĩnh)

Đã tải tarball npm vào `plans/reports/research/` (không cài global), đọc source/bundled source map của `@hyperframes/studio@0.9.23` và `hyperframes@0.8.77`, rồi xóa toàn bộ tarball/extract tạm; report không chứa upstream vendor artifacts:

- [`packages/studio/src/captions/types.ts`](https://github.com/heygen-com/hyperframes/tree/main/packages/studio/src/captions) có `CaptionSegment` (`wordId`, text, start, end, style), group và style transform.
- `src/captions/store.ts` có `updateSegmentText`, `updateSegmentTiming`, style update và history, nhưng đó là editor model/history trong memory.
- [`useCaptionSync.ts` tại pin](https://github.com/heygen-com/hyperframes/blob/dd88b5f5b78f25a82ad0677c00bbbfa85428454f/packages/studio/src/captions/hooks/useCaptionSync.ts) serialize `caption-overrides.json` chỉ với `wordId/wordIndex`, x/y/scale/rotation, color/opacity/font size/weight/family. Không serialize `text`, `start`, `end`, group/container style hoặc animation. Bản 0.8.77 trong source map có cùng giới hạn.
- [`CaptionTimeline.tsx` tại pin](https://github.com/heygen-com/hyperframes/blob/dd88b5f5b78f25a82ad0677c00bbbfa85428454f/packages/studio/src/captions/components/CaptionTimeline.tsx) ghi rõ edge-drag timing và group split đã bị bỏ vì thay đổi chỉ ở in-memory, không áp dụng playback và không serialize. Đây là bằng chứng trực tiếp chống lại yêu cầu text/timing save/reopen.
- `CaptionPropertyPanel.tsx` chỉnh transform/style trực quan và sync được phần style được hỗ trợ; đây không phải canonical transcript writer.
- `src/captions/generator.ts` tạo HTML/`TRANSCRIPT` có text/start/end và CSS. Vì vậy có thể đạt round-trip bằng cách companion/bridge patch source HTML (hoặc source sidecar rồi regenerate), nhưng không phải bằng caption sync mặc định.
- SRT implementation đã kiểm tra trong `hyperframes@0.9.23`: parser bỏ số thứ tự cue và mọi custom ID, trả `{text,start,end}`; loader sinh internal IDs `w0`, `w1`, ...; exporter đánh số output từ `1..n`. `--preserve-cues` giữ ranh giới cue theo docs/main và source 0.9.23, **không giữ stable cue ID**; khả năng flag này trong 0.8.77 vẫn là stop-gate cần smoke-test.
- `src/webmcp/useStudioAgentTools.ts` có tools generic (`studio_set_text`, `studio_set_style`, inspect/select/seek...), không có caption-specific save/export hay VoiceStudio action. `src/webmcp/polyfill.ts` nói bridge extension có thể attach vào in-page MCP, nhưng đó là automation bridge, không phải panel/button extension API.
- Studio panel registry là internal/fixed; không thấy public registration hook cho custom panel/action. [README Studio](https://github.com/heygen-com/hyperframes/blob/main/packages/studio/README.md) mô tả code editor/timeline/inspector/live preview, không mô tả third-party extension contract.

## Đối chiếu yêu cầu

| Yêu cầu | Có sẵn upstream | Cần custom |
|---|---|---|
| Mở project local, preview, refresh sau khi file đổi | Có: `preview`, local Studio/project file | Chỉ cần lifecycle wrapper/fixture |
| Chỉnh caption style/transform | Có một phần; các field trong `caption-overrides.json` được lưu | Nếu cần tất cả CSS/group/container style thì patch source hoặc companion |
| Chỉnh caption text rồi đóng/mở vẫn còn | Không chứng minh được; caption text không nằm trong override | Companion/patch source `TRANSCRIPT` hoặc generator |
| Chỉnh timing rồi đóng/mở và playback/export đổi | Không; timeline timing mutation bị bỏ và không serialize | Companion/patch source + regenerate |
| SRT import/export | Có trong source/docs đã kiểm tra; `--preserve-cues` của bản pin chưa xác nhận | Smoke-test pinned CLI; sidecar mapping để giữ stable cue ID; SRT sequence number không đủ |
| Nút `Tạo lại giọng câu này` trong Studio | Không có public extension/panel API | Fork/patch Studio, hoặc đặt nút ở companion |
| Dùng VoiceStudio backend hiện có | Không phải capability của HyperFrames | Loopback bridge gọi route hiện có, cache clip và cập nhật mapping |
| Blur subtitle nguồn + thanh ngang bán trong suốt full-width | Composition HTML/CSS cho phép; authoring khuyến nghị contrast layer | Tự author layer trong project; không có API “source subtitle blur” |

## Kiến trúc nhỏ nhất được khuyến nghị

1. Tiếp tục pin `hyperframes@0.8.77`; không dùng `latest`. Giữ project file local và mở bằng `hyperframes preview`.
2. Tạo một canonical caption document/sidecar trong project review (mỗi cue có stable `cueId`, text, start/end, style, audio clip mapping). SRT chỉ là import/export interchange; không dùng số thứ tự SRT làm ID.
3. Companion review UI trên `localhost` cùng Mac hiển thị danh sách cue và liên kết tới Studio preview. UI này ghi sidecar và cập nhật `TRANSCRIPT`/caption source HTML qua một bridge có allowlist path trong project; Studio tự refresh file. Không cấp credential cho browser/Studio.
4. Nút `Tạo lại giọng câu này` ở companion gọi code path/provider VoiceStudio hiện có (OpenAI-compatible `POST /v1/audio/speech` qua `services/localization/src/localization/providers/openai_tts.py`/router), tái sử dụng cache/clip contract hiện có. Clip mới chỉ thay mapping của cue được chọn; không chuyển sang HeyGen TTS.
5. Áp quy tắc overflow mà reviewer đã chốt: nếu duration mới không quá `1.15x` slot thì phải áp dụng time-stretch thực tế bằng cơ chế `atempo`/`playbackRate` của pipeline trước khi activate clip mới; không được chỉ kiểm tra duration rồi chấp nhận audio dài hơn. Nếu không thể áp dụng stretch hợp lệ hoặc duration vượt `1.15x`, giữ OLD active audio và lưu text đã sửa như draft mismatch.
6. Trong composition HTML, đặt source-subtitle cover phía dưới caption: full frame width (`left:0; right:0; width:100%`), nền `rgba(0,0,0,alpha)`, chiều cao theo vùng subtitle đã phát hiện; áp blur chỉ vào vùng source subtitle hoặc đặt lớp che tương ứng. Đây là code HTML/CSS của project, không phải Studio setting.

### Khi nào cần fork Studio

Chỉ chọn fork/patch nếu UX bắt buộc nút phải nằm ngay trong Studio. Fork tối thiểu cần:

- thêm caption-aware action/button và project bridge callback;
- gọi loopback endpoint chứ không giữ VoiceStudio credential trong browser;
- sửa persistence để patch canonical transcript/source (text/start/end), không mở rộng `caption-overrides.json` một cách ad-hoc;
- pin commit/package và theo dõi upstream vì Studio đang phát triển nhanh.

Companion có UX “cửa sổ review cạnh Studio” nhưng tận dụng public/local project behavior và ít surface fork hơn. WebMCP có thể làm automation adjunct, không nên xem là extension API ổn định.

## Ma trận lựa chọn

| Phương án | Tính năng | Độ phức tạp | Bảo trì/rủi ro | Xếp hạng |
|---|---|---:|---|---:|
| Companion localhost + canonical sidecar/source writer | Đủ text/timing/style, TTS nút, overflow, không fork Studio | Trung bình | Thấp-trung bình; cần bridge mới và contract test | **1** |
| Fork `@hyperframes/studio` + loopback callback | Nút nằm trong Studio, UX liền mạch | Cao | Cao; phải rebase UI/internal API, không có extension contract | 2 (chỉ khi UX bắt buộc) |
| Chỉ dùng Studio/WebMCP generic tools | Preview/style/agent automation | Thấp | Không đạt durable text/timing/TTS; WebMCP pre-stable | Không đạt |

HyperFrames phát hành rất nhanh (pin 0.8.77 nhưng registry đã 0.9.23 trong thời gian ngắn), nên adoption risk và breaking-change risk là cao hơn một editor có extension API ổn định. Docs và source cần được pin cùng version; không dựa vào undocumented internal file APIs lâu dài.

## Phase-1 stop gate / kiểm chứng cần làm trước khi triển khai

Không chạy ở nghiên cứu này theo ràng buộc. Phase 1 nên dùng fixture local, không provider thật, và dừng nếu bất kỳ điều nào sau đây không đạt:

1. Mở fixture bằng đúng `hyperframes@0.8.77`; sửa text/timing/style, đóng/mở Studio; xác nhận text/timing không tự persist (expected), style nào persist và file nào bị ghi.
2. Import/export SRT với cue boundaries; xác nhận sequence number/custom cue ID không round-trip. Chỉ tiếp tục khi sidecar `cueId` mapping được xác định.
3. Companion ghi một source/sidecar fixture và Studio refresh thấy thay đổi; bridge phải reject path ngoài project/loopback scope.
4. Contract-test VoiceStudio route bằng adapter test/no-op, chưa gọi provider thật; kiểm tra duration `<=1.15x` thì thực sự áp dụng `atempo`/`playbackRate` rồi mới activate clip mới; nếu không stretch được hoặc vượt ngưỡng, giữ clip cũ và ghi mismatch draft.
5. Nếu yêu cầu sản phẩm tuyệt đối là button bên trong Studio, dừng để quyết định ownership của fork/rebase. Nếu không, companion vẫn là phương án khuyến nghị sau khi stop-gate pass; đây chưa phải quyết định UX thay cho chủ dự án.

## Giới hạn và giả định

- Đây là source/docs/package inspection, chưa phải browser UI smoke test; vì vậy chưa tuyên bố đã kiểm chứng save roundtrip thực tế.
- Không gọi VoiceStudio, không đọc credentials, không chạy server. Route/clip/cache contract ở trên dựa trên code hiện có trong repo và cần phase-1 contract test.
- Chưa xác nhận một public, stable Studio file API cho companion ngoài documented local project behavior; bridge nên ghi file qua allowlist và coi mọi endpoint nội bộ là phụ thuộc cần smoke-test. Đây là stop gate, không nên để thành giả định ngầm.
- Báo cáo không thay đổi project code; tarball/source inspection artifacts đã được tải và xóa, không để lại upstream vendor artifacts trong workspace.

## Nguồn upstream chính

1. https://github.com/heygen-com/hyperframes/blob/main/docs/packages/cli.mdx
2. https://github.com/heygen-com/hyperframes/blob/main/docs/packages/studio.mdx
3. https://github.com/heygen-com/hyperframes/blob/main/docs/guides/captions-and-recuts.mdx
4. https://github.com/heygen-com/hyperframes/blob/main/docs/studio-app/index.mdx
5. https://github.com/heygen-com/hyperframes/blob/main/skills/media-use/audio/references/captions/authoring.md
6. https://github.com/heygen-com/hyperframes/tree/main/packages/studio/src/captions
7. https://github.com/heygen-com/hyperframes/tree/main/packages/studio/src/webmcp
8. https://github.com/heygen-com/hyperframes/blob/main/packages/studio/README.md
9. https://www.npmjs.com/package/hyperframes
