---
phase: 4
title: LaneRunner base + reaper
status: done
effort: ~0.5d
blockedBy: [2]
milestone: M1
---

# Phase 4 — Lane framework + reaper

Hiện thực Approach 1 đã chọn ở solution design §4: **vòng lặp async sống lâu, tự lên lịch**.

Đọc trước: solution design §2 (A1 vs A2 vs A3) · §4 (lý do chọn) · §10 (risk loop rò exception).

---

## 4.1 `LaneRunner` base (~80 dòng)

```ts
abstract class LaneRunner {
  abstract name: string;
  abstract idleDelayMs: number;
  abstract runOnce(signal: AbortSignal): Promise<'worked' | 'idle'>;

  // start() ở OnApplicationBootstrap, stop() ở OnApplicationShutdown
}
```

Vòng lặp:
```
while (!aborted) {
  try {
    const r = await this.runOnce(signal);
    lastTickAt = now();                       // metric
    if (r === 'idle') await sleep(idleDelayMs);
  } catch (e) {
    log(e);                                   // BẮT HẾT — không để thoát ra ngoài
    await sleep(backoff);
  }
}
```

**`try/catch` quanh toàn thân là guard quan trọng nhất của phase này.** Exception rò ra ngoài vòng lặp = lane chết **âm thầm**: không crash process, không alert, job đơn giản đứng mãi. Đây là giá phải trả khi chọn A1 thay vì `@Interval` (solution design §4 "trade-offs we accept") — nên nó có mutation test, không phải một lời nhắc.

`runOnce()` **public/protected và gọi được trực tiếp** — chính là thứ làm mutation test từng guard chạy được không cần dựng scheduler. Đây là một trong ba lý do chọn A1.

Metric `lastTickAt` per lane: alert nếu một lane không tick > 5 phút.

## 4.2 Graceful shutdown

`SIGTERM` → `abort()` → **ngừng claim việc mới**, chờ việc đang chạy xong (timeout cứng), **nhả lease** trước khi thoát. Nhả lease làm restart nhặt lại ngay thay vì chờ hết `LEASE_SECONDS`.

## 4.3 Reaper — nhịp 60s

Hai câu UPDATE ở phase 1 §1.3:
- `videos`: `downloading` + lease quá hạn → `pending` (F3)
- `jobs`: `enumerating` + lease quá hạn → `pending` (**F5**)

**F5 là lỗ trong F3 mà brainstorm bỏ sót.** Không có nó, crash giữa pha enumerate để job đứng vĩnh viễn ở `enumerating`: enumerate lane chỉ nhặt job `pending` nên job đó **không bao giờ được nhặt lại**, và tiêu chí *"kill worker → resume"* **không đạt cho pha enumerate**.

Reaper cũng là lưới an toàn cho finalize job (phase 6): job mà mọi video đã `done`/`failed`/`skipped` → `completed`.

---

## Todo

- [x] `lane.runner.ts` base + `try/catch` toàn thân + `lastTickAt`
- [x] Đăng ký `OnApplicationBootstrap` / `OnApplicationShutdown` (`lane.supervisor.ts`; shutdown dùng `beforeApplicationShutdown`, xem Outcome §2)
- [x] Graceful shutdown: ngừng claim → drain (timeout cứng) → **nhả lease**
- [x] `reaper.lane.ts`: videos (F3) + jobs (F5) + finalize job + **dọn `.tmp` quá hạn** (risk register §6)
- [x] Test: `runOnce()` gọi trực tiếp được (không cần scheduler)

## DoD

- [x] Lane chạy liên tục, exception ném ngẫu nhiên → **vẫn sống**, `lastTickAt` vẫn tiến
- [x] `SIGTERM` giữa lúc có việc → lease được nhả, restart nhặt lại **ngay** (không chờ 5 phút) — child process thật, `sigterm-shutdown.e2e-spec.ts`
- [x] **Mutation:** bỏ `try/catch` trong thân loop → test lane-survives **đỏ**
- [x] **Mutation:** bỏ reaper `videos` → test resume download **đỏ**
- [x] **Mutation:** bỏ reaper `jobs` → test resume **enumerate** **đỏ** *(F5)*
- [x] **Test kiến trúc xanh:** row `jobs`/`enumerating` nạp bằng SQL thuần → `ReaperLane` thật nhặt được, không HTTP layer nào trong đường đi; require-cache assertion giờ **thật sự load-bearing** (mutation `require` đường dẫn nối chuỗi, regex không thấy được)

## Risks

| Risk | Xử lý |
|---|---|
| Lane chết âm thầm | `try/catch` + metric `lastTickAt` + alert 5 phút. Mutation test |
| Reaper nhả video mà worker cũ vẫn đang ghi | `.tmp` tên duy nhất theo lượt claim (phase 2) + **kiểm lease trước `rename`** (phase 6) |
| Shutdown không nhả lease | Mỗi restart tốn `LEASE_SECONDS` mới tiếp tục. Có test |

---

## Outcome

**DONE.** 42 suite / 443 test xanh (6 lượt chạy liên tiếp, không flake) · `pnpm lint` xanh · gitleaks sạch · `test/mutations/phase-04.sh`: **22 entry — 19 mutation bắt bằng test, 1 bằng ESLint, 2 control phải-xanh** (đều xanh).

File mới: `src/lanes/{sleep,lane.runner,lane.supervisor,lanes.module,lanes.tokens,reaper.lane}.ts` · `src/storage/storage.module.ts` · `jobsRepository.finalizeSettled` · `Storage.removeStaleTemp` · `env.LANES_ENABLED` · `bootstrap.configureApp`.

### Bốn bug thật do test bắt, mỗi cái một test khác nhau

**1. `onModuleDestroy` đóng pool TRƯỚC khi lane nhả lease.** Nghiêm trọng nhất, và là lý do phải có child-process test. Nest tear-down theo thứ tự cố định: mọi `onModuleDestroy` → mọi `beforeApplicationShutdown` → mọi `onApplicationShutdown`. `DbModule` đóng pool ở hook đầu, `LaneSupervisor` nhả lease ở hook sau ⇒ `release()` ăn *"Cannot use a pool after calling end"*, lease giữ nguyên hết `LEASE_SECONDS`, restart ngồi cạnh việc nó không được claim — **đúng cái symptom §4.2 sinh ra để chặn**. Mọi in-process test vẫn xanh suốt, vì chúng gọi hook/`app.close()` rồi không hỏi lại xem `release` có *thành công* không. Sửa: supervisor dùng `beforeApplicationShutdown`; `DbModule`/`ParserModule`/`RedisModule` chuyển sang `onApplicationShutdown`.

**2. Lane trả `'worked'` liên tục làm chết event loop.** Vòng `while` chỉ `await` promise giải quyết trong microtask queue thì timer/socket/signal handler **không bao giờ** có lượt. API và lane **chung một process** (plan §Stack), nên lane đói = `/health` im, SIGTERM handler không chạy, container bị SIGKILL kèm lease. Phát hiện vì file test **treo** thay vì đỏ. Sửa: `setImmediate` sau mỗi tick `'worked'`.

**3. `backoffMs` khởi tạo từ `errorBackoffMs` trong field initializer.** Field initializer chạy trong constructor **lớp cha**, nên nó chốt mặc định 1000ms và mọi giá trị subclass đặt sau đó bị bỏ lặng lẽ. Sửa: derive từ số lần fail liên tiếp, không lưu.

**4. Outer catch tự nó ném được.** Nó chỉ đến được khi logger vỡ — nên `logger.error` trong đó ném tiếp, loop promise reject, cái chết thành unhandled rejection thay vì được ghi nhận. Lớp phòng thủ cuối không được phép vỡ.

### Hai bug nữa, do *đọc lại code* bắt — không có test nào đỏ

Cả hai ra từ một lượt review đối kháng sau khi suite đã xanh và mutation đã sạch.

**`releaseHeld` memoise theo *đời lane*, không theo *claim*.** Memo tồn tại để hai lời gọi `stop()` song song không cùng release (release lần hai có thể trả về `pending` một row worker khác đã claim). Nhưng promise đã resolve nằm đó vĩnh viễn ⇒ lane **restart** thì claim mới **không bao giờ** được nhả. Hôm nay không có gì restart lane trong cùng process — đúng lý do nó sẽ nằm im tới khi có. Sửa: `hold()` xoá memo, đưa nó về đúng scope là một claim.

**`isStale()` khẳng định một điều sai về lane khởi động chậm.** Nó trả true cho mọi lane chưa tick, bất kể đồng hồ nói gì — nên 60 giây sau boot, một lane có tick đầu chậm (enumerate lane chờ parser nguội) sinh ra dòng `has not ticked in over 300000ms (last tick: never)`. Câu đó **sai**, và alert sai tệ hơn không có alert vì nó dạy người vận hành bỏ qua alert thật. Giờ: chưa **start** thì stale ngay (lane supervisor bỏ sót là vấn đề thật, không có gì phải chờ); đã start mà chưa tick thì chỉ stale khi cửa sổ thực sự trôi qua.

Cả hai tìm ra bằng cách hỏi *"cái này in ra gì trong trường hợp chưa ai chạm tới"*, không phải bằng cách chạy gì. Nên làm có chủ đích ở cuối mỗi phase: mutation suite không tìm được bug trong đường code không test nào đi qua.

### Hai bug trong chính harness — cả hai đều "báo cáo sự tự tin"

**Rule 5 — harness bị *kill* phải không để lại cây nguồn nhiễm độc.** Rule 1–4 đều giả định script chạy tới được lệnh restore của nó. Một watchdog `pkill` giữa `_apply` và `restore` để lại mutation nằm nguyên trong file **chưa track** (nên `git checkout` không có gì để trả về), lượt chạy sau đo trên build đã hỏng: mutation thứ hai báo *"matched nothing"* còn mọi kết quả sau đó vô nghĩa. Sửa: snapshot pristine mọi file mục tiêu trước khi chạy + `trap ... EXIT INT TERM` restore và **nói ra** là đã restore.

**Rule 6 — mutation có thể *treo* thay vì fail.** Mutation starvation không làm test đỏ, nó làm event loop ngừng quay — nên timeout của chính jest cũng không nổ. Suite đứng vĩnh viễn tại đó. Sửa hai đầu: harness có timeout riêng và báo hang **thành một outcome riêng** (hang là catch hợp lệ cho must-fail, là *thất bại* cho control), và `lane.runner.spec.ts` có `MAX_SCRIPTED_TICKS` + probe bằng `setImmediate` chứ không bằng `setTimeout`, để test **đỏ được** thay vì treo. *Một mutation không bắt được là một guard không được canh.*

### Quyết định phát sinh

- **`LANES_ENABLED`** (mặc định `true`): làm plan §0 principle 1 có giá trị **ngay hôm nay** — tắt là node chỉ-HTTP, bật là node chạy lane, 0 dòng logic đổi. Cũng là thứ giữ mọi e2e dựng `AppModule` khỏi vô tình chạy reaper lên `public` schema của stack thật. Đọc bằng allowlist, **không** `z.coerce.boolean()` — chuỗi `"false"` truthy sẽ để lane bật trong khi config nói tắt.
- **Reaper luôn trả `'idle'`**: nhịp cố định 60s. Trả `'worked'` thì base class quay lại ngay, mà một lượt sau 1ms thì theo định nghĩa không có lease nào vừa hết.
- **Bốn sweep chạy độc lập, gom lỗi rồi mới ném**: chủ đề của phase áp cho chính reaper. Chuỗi `await` thẳng thì sweep đầu fail mỗi tick sẽ khiến ba sweep còn lại **không bao giờ** chạy nữa, không phân biệt được với reaper khoẻ.
- **Cutoff `.tmp` = một `LEASE_SECONDS`, đo bằng mtime** + registry in-flight trong process. Xoá `.tmp` của downloader còn sống làm `rename` của nó ăn ENOENT — đúng lỗi journal phase 2 mất một ngày.
