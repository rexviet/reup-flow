---
phase: 4
title: LaneRunner base + reaper
status: not_started
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

- [ ] `lane.runner.ts` base + `try/catch` toàn thân + `lastTickAt`
- [ ] Đăng ký `OnApplicationBootstrap` / `OnApplicationShutdown`
- [ ] Graceful shutdown: ngừng claim → drain → **nhả lease**
- [ ] `reaper.ts`: videos (F3) + jobs (F5) + finalize job
- [ ] Test: `runOnce()` gọi trực tiếp được (không cần scheduler)

## DoD

- [ ] Lane chạy liên tục 60s, exception ném ngẫu nhiên → **vẫn sống**, `lastTickAt` vẫn tiến
- [ ] `SIGTERM` giữa lúc có việc → lease được nhả, restart nhặt lại **ngay** (không chờ 5 phút)
- [ ] **Mutation:** bỏ `try/catch` trong thân loop → test lane-survives **đỏ**
- [ ] **Mutation:** bỏ reaper `videos` → test resume download **đỏ**
- [ ] **Mutation:** bỏ reaper `jobs` → test resume **enumerate** **đỏ** *(F5 — cái này brainstorm bỏ sót)*

## Risks

| Risk | Xử lý |
|---|---|
| Lane chết âm thầm | `try/catch` + metric `lastTickAt` + alert 5 phút. Mutation test |
| Reaper nhả video mà worker cũ vẫn đang ghi | `.tmp` tên duy nhất theo lượt claim (phase 2) + **kiểm lease trước `rename`** (phase 6) |
| Shutdown không nhả lease | Mỗi restart tốn `LEASE_SECONDS` mới tiếp tục. Có test |
