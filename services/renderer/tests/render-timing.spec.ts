// Where the time of a render goes: each phase measured, in order, adding up to the total,
// logged once and traced as child spans, with durations and a hash prefix and nothing else.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { Tracer } from "@opentelemetry/api";
import { createRenderActivity, RenderInvocations } from "../src/activities/render";
import { specSha256 } from "../src/edit-spec";
import type { Clock } from "../src/phase-timer";
import type { RenderVideo } from "../src/remotion-render";
import {
  AWEME,
  FONT_BYTES,
  FONT_NAME,
  makeSpec,
  SPEC_KEY,
  storeWith,
  MemoryStore,
} from "./support/memory-store";

class FakeClock implements Clock {
  t = 1000;
  now(): number {
    return this.t;
  }
  advance(ms: number): void {
    this.t += ms;
  }
}

interface RecordedSpan {
  name: string;
  startTime: unknown;
  attributes: Record<string, unknown>;
  status?: unknown;
  ended: unknown;
}

function fakeTracer(spans: RecordedSpan[]): Tracer {
  return {
    startSpan(name: string, options?: { startTime?: unknown }) {
      const rec: RecordedSpan = {
        name,
        startTime: options?.startTime,
        attributes: {},
        ended: undefined,
      };
      spans.push(rec);
      return {
        setAttribute(k: string, v: unknown) {
          rec.attributes[k] = v;
          return this;
        },
        setStatus(s: unknown) {
          rec.status = s;
          return this;
        },
        end(t: unknown) {
          rec.ended = t;
        },
      };
    },
  } as unknown as Tracer;
}

let tmp: string;
let fontPath: string;
beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "render-timing-"));
  fontPath = path.join(tmp, FONT_NAME);
  fs.writeFileSync(fontPath, FONT_BYTES);
  fs.mkdirSync(path.join(tmp, "work"));
});
afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

const input = { aweme_id: AWEME, spec_key: SPEC_KEY };

/** A store whose every call takes a set time on the fake clock. */
function slowStore(clock: FakeClock, cost: Record<string, number>): MemoryStore {
  const store = storeWith();
  const wrap = <K extends "getBytes" | "download" | "exists" | "uploadFile">(name: K) => {
    const original = store[name].bind(store) as (...a: unknown[]) => Promise<unknown>;
    (store as unknown as Record<string, unknown>)[name] = async (...args: unknown[]) => {
      clock.advance(cost[name]);
      return original(...args);
    };
  };
  for (const name of ["getBytes", "download", "exists", "uploadFile"] as const) wrap(name);
  return store;
}

function setup(clock: FakeClock, store: MemoryStore, renderVideo: RenderVideo) {
  const spans: RecordedSpan[] = [];
  const lines: string[] = [];
  const render = createRenderActivity({
    store,
    renderVideo,
    fontPath,
    invocations: new RenderInvocations(),
    heartbeat: () => undefined,
    tmpRoot: path.join(tmp, "work"),
    clock,
    tracer: fakeTracer(spans),
    log: (l) => lines.push(l),
  });
  return { render, spans, lines };
}

const COST = { getBytes: 7, exists: 3, download: 100, uploadFile: 50 };

const phased =
  (clock: FakeClock): RenderVideo =>
  async ({ outputLocation, timer }) => {
    await timer!.time("select", async () => clock.advance(30));
    const start = timer!.now();
    clock.advance(400);
    timer!.mark("first_frame");
    timer!.mark("last_frame");
    const drawn = timer!.now();
    clock.advance(70);
    timer!.record("frames", start, drawn);
    timer!.record("encode", drawn, timer!.now());
    fs.writeFileSync(outputLocation, "mp4");
  };

describe("the time of a render", () => {
  it("is recorded phase by phase, in order, and adds up to the total", async () => {
    const clock = new FakeClock();
    const { render, lines, spans } = setup(clock, slowStore(clock, COST), phased(clock));
    await render(input);
    expect(spans.map((s) => s.name)).toEqual([
      "render.spec",
      "render.exists",
      "render.fetch",
      "render.select",
      "render.frames",
      "render.encode",
      "render.upload",
    ]);
    expect(lines).toHaveLength(1);
    const line = JSON.parse(lines[0]);
    expect(line).toMatchObject({
      event: "render.timing",
      spec_ms: 7,
      exists_ms: 3,
      fetch_ms: 200,
      select_ms: 30,
      frames_ms: 400,
      encode_ms: 70,
      upload_ms: 50,
      total_ms: 760,
    });
    const phases = ["spec", "exists", "fetch", "select", "frames", "encode", "upload"];
    expect(phases.reduce((sum, p) => sum + line[`${p}_ms`], 0)).toBe(line.total_ms);
    expect(line.first_frame_ms).toBeLessThanOrEqual(line.last_frame_ms);
    expect(line.error_type).toBeUndefined();
  });

  it("still records the phases that ran, and the error type, when one fails", async () => {
    const clock = new FakeClock();
    const failing: RenderVideo = async ({ timer }) => {
      await timer!.time("select", async () => {
        clock.advance(30);
        throw Object.assign(new Error("no browser"), { type: "BrowserDied" });
      });
    };
    const { render, lines, spans } = setup(clock, slowStore(clock, COST), failing);
    await expect(render(input)).rejects.toThrow("no browser");
    const line = JSON.parse(lines[0]);
    expect(line).toMatchObject({
      spec_ms: 7,
      exists_ms: 3,
      fetch_ms: 200,
      select_ms: 30,
      error_type: "BrowserDied",
    });
    expect(line.upload_ms).toBeUndefined();
    const select = spans.find((s) => s.name === "render.select")!;
    expect(select.attributes["error.type"]).toBe("BrowserDied");
    expect(select.status).toBeDefined();
  });

  it("is logged for a render that was already made, with only what it did", async () => {
    const clock = new FakeClock();
    const store = slowStore(clock, COST);
    store.put(`work/${AWEME}/render-${specSha256(makeSpec())}.mp4`, "earlier");
    const { render, lines } = setup(clock, store, phased(clock));
    await render(input);
    const line = JSON.parse(lines[0]);
    expect(line).toMatchObject({ spec_ms: 7, exists_ms: 3, total_ms: 10 });
    expect(line.fetch_ms).toBeUndefined();
  });

  it("puts nothing in the log or on a span but durations, a hash prefix and an error type", async () => {
    const clock = new FakeClock();
    const spec = makeSpec();
    const { render, lines, spans } = setup(clock, slowStore(clock, COST), phased(clock));
    await render(input);
    const everything = JSON.stringify({ lines, spans });
    const forbidden = [
      AWEME,
      "work/",
      "http",
      "127.0.0.1",
      tmp,
      spec.source_key,
      spec.audio_key,
      spec.captions[0].text,
      FONT_NAME,
      specSha256(spec), // the whole hash: only its prefix may appear
    ];
    for (const word of forbidden) expect(everything).not.toContain(word);
    expect(lines[0]).toContain(specSha256(spec).slice(0, 12));
    for (const s of spans) {
      expect(Object.keys(s.attributes)).toEqual(["render.duration_ms"]);
      expect(typeof s.attributes["render.duration_ms"]).toBe("number");
    }
  });
});
