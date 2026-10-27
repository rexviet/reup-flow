// The activity around `renderMedia`: what it keys the output by, what it reads and from
// where, and what it leaves behind. The renderer itself is a stand-in here; the real one
// is exercised by render.integration-spec.ts.
import * as fs from "node:fs";
import * as http from "node:http";
import * as os from "node:os";
import * as path from "node:path";
import {
  createRenderActivity,
  RENDER_DEADLINE_BASE_MS,
  RENDER_DEADLINE_PER_VIDEO_MS,
  renderDeadlineMs,
  renderKey,
  RenderInvocations,
} from "../src/activities/render";
import { specSha256 } from "../src/edit-spec";
import type { RenderVideo, RenderVideoArgs } from "../src/remotion-render";
import {
  AUDIO_BYTES,
  AWEME,
  FONT_BYTES,
  FONT_NAME,
  makeSpec,
  SOURCE_BYTES,
  SPEC_KEY,
  storeWith,
  MemoryStore,
} from "./support/memory-store";

let tmp: string;
let fontPath: string;

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "render-spec-"));
  fontPath = path.join(tmp, FONT_NAME);
  fs.writeFileSync(fontPath, FONT_BYTES);
});

afterEach(() => fs.rmSync(tmp, { recursive: true, force: true }));

function fetchUrl(url: string): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    http
      .get(url, { agent: false }, (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => resolve(Buffer.concat(chunks)));
      })
      .on("error", reject);
  });
}

// What a render needs of its stand-in: write a file where it is told to.
const writes =
  (content = "mp4-bytes"): RenderVideo =>
  async ({ outputLocation, onProgress }) => {
    onProgress(0.5);
    fs.writeFileSync(outputLocation, content);
  };

function setup(store: MemoryStore, renderVideo: RenderVideo = writes()) {
  const invocations = new RenderInvocations();
  const heartbeats: number[] = [];
  const tmpRoot = path.join(tmp, "work");
  fs.mkdirSync(tmpRoot);
  const render = createRenderActivity({
    store,
    renderVideo,
    fontPath,
    invocations,
    heartbeat: (p) => heartbeats.push(p),
    tmpRoot,
  });
  return { render, invocations, heartbeats, tmpRoot };
}

const input = { aweme_id: AWEME, spec_key: SPEC_KEY };

describe("what a render is keyed by", () => {
  it("is the hash of the spec", async () => {
    const spec = makeSpec();
    const store = storeWith(spec);
    const { render } = setup(store);
    const out = await render(input);
    expect(out.spec_sha256).toBe(specSha256(spec));
    expect(out.render_key).toBe(`work/${AWEME}/render-${specSha256(spec)}.mp4`);
    expect(out.render_key).toBe(renderKey(AWEME, specSha256(spec)));
    expect(store.objects.get(out.render_key)?.toString()).toBe("mp4-bytes");
  });

  it("does not render a spec that is already rendered, and says where it is", async () => {
    const store = storeWith();
    const { render, invocations } = setup(store);
    const first = await render(input);
    const uploadsAfterFirst = store.uploads.length;
    const downloadsAfterFirst = store.downloads.length;
    const second = await render(input);
    expect(second).toEqual(first);
    expect(invocations.count(first.spec_sha256)).toBe(1);
    expect(store.uploads).toHaveLength(uploadsAfterFirst);
    expect(store.downloads).toHaveLength(downloadsAfterFirst);
  });

  it("finds an earlier render by its key alone, with nothing else recorded", async () => {
    const spec = makeSpec();
    const store = storeWith(spec);
    store.put(renderKey(AWEME, specSha256(spec)), "earlier");
    const { render, invocations } = setup(store);
    const out = await render(input);
    expect(invocations.count(out.spec_sha256)).toBe(0);
    expect(store.objects.get(out.render_key)?.toString()).toBe("earlier");
  });

  it("renders again, under another key, when a caption changed", async () => {
    const store = storeWith();
    const { render, invocations } = setup(store);
    const first = await render(input);
    const edited = makeSpec({
      captions: [
        {
          segment_id: 0,
          start_ms: 0,
          end_ms: 3000,
          text: "Tạm biệt",
          font_px: 48,
        },
      ],
    });
    store.put(SPEC_KEY, JSON.stringify(edited));
    const second = await render(input);
    expect(second.render_key).not.toBe(first.render_key);
    expect(invocations.count(first.spec_sha256)).toBe(1);
    expect(invocations.count(second.spec_sha256)).toBe(1);
    expect(store.objects.has(first.render_key)).toBe(true);
    expect(store.objects.has(second.render_key)).toBe(true);
  });

  it("renders again when only the cover colour changed", async () => {
    const store = storeWith();
    const { render } = setup(store);
    const first = await render(input);
    const spec = makeSpec();
    store.put(
      SPEC_KEY,
      JSON.stringify({ ...spec, cover: { ...spec.cover!, color: "#ffffff" } }),
    );
    expect((await render(input)).render_key).not.toBe(first.render_key);
  });
});

describe("what a render reads", () => {
  it("gets the source, the audio and the font from a loopback HTTP server, not from paths", async () => {
    const store = storeWith();
    const seen: Record<string, Buffer> = {};
    const urls: string[] = [];
    const { render } = setup(store, async (args: RenderVideoArgs) => {
      for (const [name, url] of Object.entries(args.urls)) {
        urls.push(url);
        seen[name] = await fetchUrl(url);
      }
      fs.writeFileSync(args.outputLocation, "mp4");
    });
    await render(input);
    expect(seen.source).toEqual(SOURCE_BYTES);
    expect(seen.audio).toEqual(AUDIO_BYTES);
    expect(seen.font).toEqual(FONT_BYTES);
    for (const url of urls) expect(new URL(url).hostname).toBe("127.0.0.1");
  });

  it("gives each job a server of its own", async () => {
    const bases: string[] = [];
    const store = storeWith();
    const { render } = setup(store, async (args) => {
      bases.push(args.urls.source.split("/").slice(0, 4).join("/"));
      fs.writeFileSync(args.outputLocation, "mp4");
    });
    await render(input);
    store.objects.delete(renderKey(AWEME, specSha256(makeSpec())));
    await render(input);
    expect(new Set(bases).size).toBe(2);
  });

  it("passes the spec it read to the renderer unchanged", async () => {
    const spec = makeSpec();
    let received: unknown;
    const { render } = setup(storeWith(spec), async (args) => {
      received = args.spec;
      fs.writeFileSync(args.outputLocation, "mp4");
    });
    await render(input);
    expect(received).toEqual(spec);
  });

  it("tells Temporal it is alive while rendering", async () => {
    const { render, heartbeats } = setup(storeWith());
    await render(input);
    expect(heartbeats).toEqual([0.5]);
  });
});

describe("what a render leaves behind", () => {
  it("removes its folder and stops its server when it succeeds", async () => {
    const store = storeWith();
    let base = "";
    const { render, tmpRoot } = setup(store, async (args) => {
      base = args.urls.source;
      fs.writeFileSync(args.outputLocation, "mp4");
    });
    await render(input);
    expect(fs.readdirSync(tmpRoot)).toEqual([]);
    await expect(fetchUrl(base)).rejects.toMatchObject({
      code: "ECONNREFUSED",
    });
  });

  it("removes its folder, stops its server and uploads nothing when the renderer fails", async () => {
    const store = storeWith();
    let base = "";
    const { render, tmpRoot } = setup(store, async (args) => {
      base = args.urls.source;
      throw new Error("chromium crashed");
    });
    await expect(render(input)).rejects.toThrow("chromium crashed");
    expect(fs.readdirSync(tmpRoot)).toEqual([]);
    await expect(fetchUrl(base)).rejects.toMatchObject({
      code: "ECONNREFUSED",
    });
    expect(store.uploads).toEqual([]);
  });

  it("leaves nothing to find after a failure, so a retry renders", async () => {
    const store = storeWith();
    let attempts = 0;
    const { render, invocations } = setup(store, async (args) => {
      attempts += 1;
      if (attempts === 1) throw new Error("chromium crashed");
      fs.writeFileSync(args.outputLocation, "mp4");
    });
    await expect(render(input)).rejects.toThrow();
    const out = await render(input);
    expect(store.objects.has(out.render_key)).toBe(true);
    expect(invocations.count(out.spec_sha256)).toBe(2);
  });
});

describe("what a render refuses, for good", () => {
  async function refused(run: () => Promise<unknown>) {
    try {
      await run();
    } catch (err) {
      return err as { type?: string; nonRetryable?: boolean };
    }
    throw new Error("expected the render to be refused");
  }

  it("a track that is not the one the spec was made for", async () => {
    const store = storeWith();
    store.put(makeSpec().audio_key, "another track");
    const { render, invocations } = setup(store);
    const err = await refused(() => render(input));
    expect([err.type, err.nonRetryable]).toEqual(["AudioHashMismatch", true]);
    expect(store.uploads).toEqual([]);
    expect(invocations.count(specSha256(makeSpec()))).toBe(0);
  });

  it("a spec laid out with another font than this renderer has", async () => {
    const store = storeWith(makeSpec({ font: "Other.ttf" }));
    const { render } = setup(store);
    const err = await refused(() => render(input));
    expect([err.type, err.nonRetryable]).toEqual(["FontMismatch", true]);
    expect(store.uploads).toEqual([]);
  });

  it("a spec the schema does not accept, before anything is downloaded", async () => {
    const store = storeWith();
    store.put(SPEC_KEY, JSON.stringify({ ...makeSpec(), fps: 0 }));
    const { render } = setup(store);
    const err = await refused(() => render(input));
    expect([err.type, err.nonRetryable]).toEqual(["InvalidEditSpec", true]);
    expect(store.downloads).toEqual([]);
  });

  it("a spec that points at another video's files", async () => {
    const store = storeWith(
      makeSpec({ source_key: "work/9999/r0/source.mp4" }),
    );
    const { render } = setup(store);
    const err = await refused(() => render(input));
    expect([err.type, err.nonRetryable]).toEqual(["InvalidEditSpec", true]);
    expect(store.downloads).toEqual([]);
  });

  it.each([
    [
      "an aweme id that is not digits",
      { aweme_id: "../1", spec_key: SPEC_KEY },
    ],
    [
      "a spec outside the video's folder",
      { aweme_id: AWEME, spec_key: "work/9999/r1/edit-spec.json" },
    ],
    [
      "a spec key that climbs out",
      { aweme_id: AWEME, spec_key: `work/${AWEME}/../9999/x.json` },
    ],
  ])("%s", async (_label, bad) => {
    const { render } = setup(storeWith());
    const err = await refused(() => render(bad));
    expect([err.type, err.nonRetryable]).toEqual(["InvalidInput", true]);
  });

  it("a missing input is not retried either", async () => {
    const store = storeWith();
    store.objects.delete(makeSpec().audio_key);
    const { render } = setup(store);
    const err = await refused(() => render(input));
    expect(err.nonRetryable).toBe(true);
  });
});

describe("what a render does when two ask for the same spec at once", () => {
  // A render that holds until the test lets it go, so the second request arrives while
  // the first is running.
  function gated() {
    let started!: () => void;
    const hasStarted = new Promise<void>((resolve) => (started = resolve));
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const renderVideo: RenderVideo = async ({ outputLocation }) => {
      started();
      await gate;
      fs.writeFileSync(outputLocation, "mp4");
    };
    return { renderVideo, hasStarted, release };
  }

  it("renders it once and gives both the same answer", async () => {
    const store = storeWith();
    const g = gated();
    const { render, invocations } = setup(store, g.renderVideo);
    const first = render(input);
    await g.hasStarted;
    const second = render(input);
    // Give the second request every chance to start a render of its own.
    await new Promise((resolve) => setTimeout(resolve, 100));
    g.release();
    const [a, b] = await Promise.all([first, second]);
    expect(b).toEqual(a);
    expect(invocations.count(a.spec_sha256)).toBe(1);
    expect(store.uploads).toEqual([a.render_key]);
  });

  it("control: two different specs at once do overlap", async () => {
    const store = storeWith();
    const other = makeSpec({
      captions: [
        {
          segment_id: 0,
          start_ms: 0,
          end_ms: 3000,
          text: "Tạm biệt",
          font_px: 48,
        },
      ],
    });
    store.put(`work/${AWEME}/r2/edit-spec.json`, JSON.stringify(other));
    let running = 0;
    let peak = 0;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const { render } = setup(store, async ({ outputLocation }) => {
      running += 1;
      peak = Math.max(peak, running);
      await gate;
      running -= 1;
      fs.writeFileSync(outputLocation, "mp4");
    });
    const both = Promise.all([
      render(input),
      render({ aweme_id: AWEME, spec_key: `work/${AWEME}/r2/edit-spec.json` }),
    ]);
    await new Promise((resolve) => setTimeout(resolve, 150));
    release();
    await both;
    expect(peak).toBe(2);
  });

  it("lets the same spec be rendered again once the first render has finished and failed", async () => {
    const store = storeWith();
    let attempts = 0;
    const { render } = setup(store, async ({ outputLocation }) => {
      attempts += 1;
      if (attempts === 1) throw new Error("chromium crashed");
      fs.writeFileSync(outputLocation, "mp4");
    });
    await expect(render(input)).rejects.toThrow("chromium crashed");
    await expect(render(input)).resolves.toMatchObject({
      spec_sha256: expect.any(String),
    });
  });
});

describe("what a render shared by two callers does when one of them goes", () => {
  // Two callers of one spec, each with a cancellation of its own, on one render that
  // holds until the test lets it go.
  function shared() {
    const store = storeWith();
    const first = new AbortController();
    const second = new AbortController();
    const turn: { current: AbortController } = { current: first };
    const beats: number[] = [];
    let started!: () => void;
    const hasStarted = new Promise<void>((resolve) => (started = resolve));
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    let renderSignal: AbortSignal | undefined;
    const tmpRoot = path.join(tmp, "shared");
    fs.mkdirSync(tmpRoot);
    const invocations = new RenderInvocations();
    const render = createRenderActivity({
      store,
      fontPath,
      invocations,
      heartbeat: (p) => beats.push(p),
      heartbeatIntervalMs: 10,
      tmpRoot,
      cancellation: () => turn.current.signal,
      renderVideo: async ({ outputLocation, signal }) => {
        renderSignal = signal;
        started();
        await Promise.race([
          gate,
          new Promise<never>((_resolve, reject) =>
            signal?.addEventListener(
              "abort",
              () => reject(new Error("render stopped")),
              {
                once: true,
              },
            ),
          ),
        ]);
        fs.writeFileSync(outputLocation, "mp4");
      },
    });
    const callFirst = () => {
      turn.current = first;
      return render(input);
    };
    const callSecond = () => {
      turn.current = second;
      return render(input);
    };
    return {
      store,
      first,
      second,
      beats,
      hasStarted,
      release,
      tmpRoot,
      invocations,
      callFirst,
      callSecond,
      renderSignal: () => renderSignal,
    };
  }
  const settle = () => new Promise((resolve) => setTimeout(resolve, 80));

  it("keeps rendering for the one that stays, and hands it the result", async () => {
    const s = shared();
    const a = s.callFirst();
    await s.hasStarted;
    const b = s.callSecond();
    await settle();
    s.first.abort(new Error("first cancelled"));
    await expect(a).rejects.toThrow("first cancelled");
    await settle();
    expect(s.renderSignal()?.aborted).toBe(false);
    s.release();
    const out = await b;
    expect(s.store.objects.get(out.render_key)?.toString()).toBe("mp4");
    expect(s.invocations.count(out.spec_sha256)).toBe(1);
  });

  it("keeps rendering for the first when only the one that joined goes", async () => {
    const s = shared();
    const a = s.callFirst();
    await s.hasStarted;
    const b = s.callSecond();
    await settle();
    s.second.abort(new Error("second cancelled"));
    await expect(b).rejects.toThrow("second cancelled");
    expect(s.renderSignal()?.aborted).toBe(false);
    s.release();
    await expect(a).resolves.toMatchObject({ spec_sha256: expect.any(String) });
  });

  it("stops the render, and leaves nothing, when the last one has gone", async () => {
    const s = shared();
    const a = s.callFirst();
    await s.hasStarted;
    const b = s.callSecond();
    await settle();
    s.first.abort(new Error("first cancelled"));
    await expect(a).rejects.toThrow("first cancelled");
    s.second.abort(new Error("second cancelled"));
    await expect(b).rejects.toThrow("second cancelled");
    expect(s.renderSignal()?.aborted).toBe(true);
    expect(fs.readdirSync(s.tmpRoot)).toEqual([]);
    expect(s.store.uploads).toEqual([]);
  });

  it("a caller that is cancelled stops waiting even though the render goes on", async () => {
    const s = shared();
    const a = s.callFirst();
    await s.hasStarted;
    const b = s.callSecond();
    await settle();
    s.second.abort(new Error("second cancelled"));
    // It must not wait for the render, which is still held.
    await expect(b).rejects.toThrow("second cancelled");
    s.release();
    await a;
  });

  it("keeps the one that stays alive with heartbeats of its own", async () => {
    const s = shared();
    const a = s.callFirst();
    await s.hasStarted;
    const b = s.callSecond();
    await settle();
    s.first.abort(new Error("first cancelled"));
    await expect(a).rejects.toThrow();
    const before = s.beats.length;
    await settle();
    expect(s.beats.length).toBeGreaterThan(before);
    s.release();
    await b;
  });

  it("a caller that comes while the last one is stopping the render starts its own", async () => {
    const s = shared();
    const a = s.callFirst();
    await s.hasStarted;
    s.first.abort(new Error("first cancelled"));
    await expect(a).rejects.toThrow("first cancelled");
    // The first render is over (the caller waited for it), so the next one is new.
    s.release();
    const again = await s.callSecond();
    expect(s.store.objects.get(again.render_key)?.toString()).toBe("mp4");
    expect(s.invocations.count(again.spec_sha256)).toBe(2);
  });
});

describe("how long a render is given", () => {
  it("grows with the length of the video, from a fixed start", () => {
    expect(renderDeadlineMs(0)).toBe(RENDER_DEADLINE_BASE_MS);
    expect(renderDeadlineMs(60_000)).toBe(
      RENDER_DEADLINE_BASE_MS + 60_000 * RENDER_DEADLINE_PER_VIDEO_MS,
    );
  });

  it("gives the longest video the spec allows more than the 3x real time it was measured at", () => {
    const longest = 15 * 60 * 1000;
    expect(renderDeadlineMs(longest)).toBeGreaterThan(3 * longest);
    expect(renderDeadlineMs(longest)).toBeGreaterThanOrEqual(2 * 3 * longest);
  });

  it("ends a render with the deadline, for good, even when the time ran out while downloading", async () => {
    const store = storeWith();
    const slow = Object.create(store) as MemoryStore;
    slow.download = async (key, destination) => {
      await new Promise((resolve) => setTimeout(resolve, 120));
      return store.download(key, destination);
    };
    const render = createRenderActivity({
      store: slow,
      fontPath,
      invocations: new RenderInvocations(),
      heartbeat: () => undefined,
      tmpRoot: tmp,
      deadlineMs: 30,
      renderVideo: async () => {
        throw new Error("must not get here");
      },
    });
    await expect(render(input)).rejects.toMatchObject({
      type: "RenderDeadline",
      nonRetryable: true,
    });
    expect(store.uploads).toEqual([]);
  });
});

describe("what stops a render", () => {
  it("passes the activity's cancellation to the renderer, which stops and leaves nothing", async () => {
    const store = storeWith();
    const controller = new AbortController();
    let seen: AbortSignal | undefined;
    let started!: () => void;
    const hasStarted = new Promise<void>((resolve) => (started = resolve));
    const invocations = new RenderInvocations();
    const tmpRoot = path.join(tmp, "work");
    fs.mkdirSync(tmpRoot);
    const render = createRenderActivity({
      store,
      fontPath,
      invocations,
      heartbeat: () => undefined,
      tmpRoot,
      cancellation: () => controller.signal,
      renderVideo: async ({ signal }) => {
        seen = signal;
        started();
        await new Promise<never>((_resolve, reject) =>
          signal?.addEventListener(
            "abort",
            () => reject(new Error("cancelled")),
            { once: true },
          ),
        );
      },
    });
    const running = render(input);
    await hasStarted;
    controller.abort(new Error("cancelled"));
    await expect(running).rejects.toThrow("cancelled");
    expect(seen?.aborted).toBe(true);
    expect(fs.readdirSync(tmpRoot)).toEqual([]);
    expect(store.uploads).toEqual([]);
  });

  it("gives up for good on a render that outlives its deadline", async () => {
    const store = storeWith();
    const render = createRenderActivity({
      store,
      fontPath,
      invocations: new RenderInvocations(),
      heartbeat: () => undefined,
      tmpRoot: tmp,
      deadlineMs: 50,
      renderVideo: ({ signal }) =>
        new Promise<never>((_resolve, reject) =>
          signal?.addEventListener(
            "abort",
            () => reject(new Error("aborted")),
            { once: true },
          ),
        ),
    });
    try {
      await render(input);
      throw new Error("expected the deadline to end the render");
    } catch (err) {
      const failure = err as { type?: string; nonRetryable?: boolean };
      expect([failure.type, failure.nonRetryable]).toEqual([
        "RenderDeadline",
        true,
      ]);
    }
    expect(store.uploads).toEqual([]);
  });

  it("heartbeats while it downloads, before the renderer has reported anything", async () => {
    const store = storeWith();
    const slow = Object.create(store) as MemoryStore;
    slow.download = async (key, destination) => {
      await new Promise((resolve) => setTimeout(resolve, 150));
      return store.download(key, destination);
    };
    const beats: number[] = [];
    let beatsWhenRenderStarted = -1;
    const render = createRenderActivity({
      store: slow,
      fontPath,
      invocations: new RenderInvocations(),
      heartbeat: (p) => beats.push(p),
      heartbeatIntervalMs: 20,
      tmpRoot: tmp,
      renderVideo: async ({ outputLocation }) => {
        beatsWhenRenderStarted = beats.length;
        fs.writeFileSync(outputLocation, "mp4");
      },
    });
    await render(input);
    expect(beatsWhenRenderStarted).toBeGreaterThanOrEqual(3);
  });

  it("stops heartbeating when the render is over", async () => {
    const beats: number[] = [];
    const render = createRenderActivity({
      store: storeWith(),
      fontPath,
      invocations: new RenderInvocations(),
      heartbeat: (p) => beats.push(p),
      heartbeatIntervalMs: 10,
      tmpRoot: tmp,
      renderVideo: writes(),
    });
    await render(input);
    const after = beats.length;
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(beats).toHaveLength(after);
  });
});

describe("what else a render refuses, for good", () => {
  it("a spec that is not JSON", async () => {
    const store = storeWith();
    store.put(SPEC_KEY, "{not json");
    const { render } = setup(store);
    await expect(render(input)).rejects.toMatchObject({
      type: "InvalidEditSpec",
      nonRetryable: true,
    });
  });

  it("a font with the right name and other bytes", async () => {
    fs.writeFileSync(fontPath, Buffer.concat([FONT_BYTES, Buffer.from("!")]));
    const store = storeWith();
    const { render } = setup(store);
    await expect(render(input)).rejects.toMatchObject({
      type: "FontMismatch",
      nonRetryable: true,
    });
    expect(store.downloads).toEqual([]);
  });
});
