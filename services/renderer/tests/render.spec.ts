// The activity around `renderMedia`: what it keys the output by, what it reads and from
// where, and what it leaves behind. The renderer itself is a stand-in here; the real one
// is exercised by render.integration-spec.ts.
import * as fs from "node:fs";
import * as http from "node:http";
import * as os from "node:os";
import * as path from "node:path";
import { createRenderActivity, renderKey, RenderInvocations } from "../src/activities/render";
import { specSha256 } from "../src/edit-spec";
import type { RenderVideo, RenderVideoArgs } from "../src/remotion-render";
import {
  AUDIO_BYTES,
  AWEME,
  FONT_NAME,
  makeSpec,
  SOURCE_BYTES,
  SPEC_KEY,
  storeWith,
  MemoryStore,
} from "./support/memory-store";

const FONT_BYTES = Buffer.from("pretend this is a font");
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
      captions: [{ segment_id: 0, start_ms: 0, end_ms: 3000, text: "Tạm biệt", font_px: 48 }],
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
    store.put(SPEC_KEY, JSON.stringify({ ...spec, cover: { ...spec.cover!, color: "#ffffff" } }));
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
    await expect(fetchUrl(base)).rejects.toMatchObject({ code: "ECONNREFUSED" });
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
    await expect(fetchUrl(base)).rejects.toMatchObject({ code: "ECONNREFUSED" });
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
    const store = storeWith(makeSpec({ source_key: "work/9999/r0/source.mp4" }));
    const { render } = setup(store);
    const err = await refused(() => render(input));
    expect([err.type, err.nonRetryable]).toEqual(["InvalidEditSpec", true]);
    expect(store.downloads).toEqual([]);
  });

  it.each([
    ["an aweme id that is not digits", { aweme_id: "../1", spec_key: SPEC_KEY }],
    ["a spec outside the video's folder", { aweme_id: AWEME, spec_key: "work/9999/r1/edit-spec.json" }],
    ["a spec key that climbs out", { aweme_id: AWEME, spec_key: `work/${AWEME}/../9999/x.json` }],
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
