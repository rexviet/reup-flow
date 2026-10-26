// The real thing: a HEVC clip, the real composition in Chromium, ffmpeg, and an S3 store
// reached with the renderer's own credential (work/* only).
//
// Needs S3_ENDPOINT, RENDER_S3_ACCESS_KEY_ID, RENDER_S3_SECRET_ACCESS_KEY (and S3_BUCKET
// when it is not `reup-media`). MinIO publishes no S3 port; point S3_ENDPOINT at a loopback
// proxy onto the compose network.
import * as fs from "node:fs";
import * as path from "node:path";
import { createHash, randomInt } from "node:crypto";
import { createRenderActivity, renderKey, RenderInvocations } from "../src/activities/render";
import { loadSettings } from "../src/config";
import { S3ObjectStore } from "../src/object-store";
import { bundleComposition, remotionRenderer } from "../src/remotion-render";
import { frameAt, durationInFrames } from "../src/frames";
import type { EditSpec } from "../src/edit-spec";
import { specSha256 } from "../src/edit-spec";
import { FONT_PATH, frameAt as decodeFrame, makeHevcFixture, makeTone, probe, tmpDir } from "./support/media";

for (const name of ["S3_ENDPOINT", "RENDER_S3_ACCESS_KEY_ID", "RENDER_S3_SECRET_ACCESS_KEY"]) {
  if (!process.env[name]) {
    throw new Error(`${name} is not set: this suite needs the renderer's own MinIO user`);
  }
}

const settings = loadSettings({ ...process.env, CAPTION_FONT_PATH: FONT_PATH });
const store = S3ObjectStore.fromSettings(settings.s3);
// A video id no other run shares; the credential can write anywhere under work/.
const AWEME = `99${Date.now()}${randomInt(1000, 9999)}`;
const FPS = 30;
const COVER = { x: 40, y: 1500, w: 1000, h: 120 };
const COVER_COLOR = [0x20, 0x40, 0xc0] as const;

let dir: string;
let spec: EditSpec;
let invocations: RenderInvocations;
let uploads: string[];
let render: ReturnType<typeof createRenderActivity>;
let realClip: boolean;
const specKey = `work/${AWEME}/r1/edit-spec.json`;

beforeAll(async () => {
  dir = tmpDir("render-it-");
  const source = makeHevcFixture(dir);
  realClip = source.real;
  const audio = makeTone(dir, 3);
  await store.uploadFile(`work/${AWEME}/r0/source.mp4`, source.file, "video/mp4");
  await store.uploadFile(`work/${AWEME}/r1/dubbed.wav`, audio, "audio/wav");
  spec = {
    source_key: `work/${AWEME}/r0/source.mp4`,
    audio_key: `work/${AWEME}/r1/dubbed.wav`,
    audio_sha256: createHash("sha256").update(fs.readFileSync(audio)).digest("hex"),
    script_version: 1,
    width: 1080,
    height: 1920,
    fps: FPS,
    duration_ms: 3000,
    cover: {
      region: COVER,
      color: `#${COVER_COLOR.map((c) => c.toString(16).padStart(2, "0")).join("")}`,
      opacity_pct: 100,
    },
    caption_box: COVER,
    captions: [
      { segment_id: 0, start_ms: 0, end_ms: 3000, text: "Xin chào các bạn", font_px: 48 },
    ],
    font: path.basename(FONT_PATH),
  };
  const specFile = path.join(dir, "edit-spec.json");
  fs.writeFileSync(specFile, JSON.stringify(spec));
  await store.uploadFile(specKey, specFile, "application/json");

  invocations = new RenderInvocations();
  uploads = [];
  const counting: S3ObjectStore = Object.assign(Object.create(store), {
    uploadFile: async (key: string, file: string, type: string) => {
      uploads.push(key);
      return store.uploadFile(key, file, type);
    },
  });
  render = createRenderActivity({
    store: counting,
    renderVideo: remotionRenderer(await bundleComposition()),
    fontPath: FONT_PATH,
    invocations,
  });
});

afterAll(() => fs.rmSync(dir, { recursive: true, force: true }));

describe("rendering a spec for real", () => {
  let output: string;
  let first: Awaited<ReturnType<typeof render>>;

  beforeAll(async () => {
    first = await render({ aweme_id: AWEME, spec_key: specKey });
    output = path.join(dir, "output.mp4");
    await store.download(first.render_key, output);
  });

  it("starts from a HEVC video (a real Douyin clip when data/ has one)", () => {
    const sourceCodec = probe(path.join(dir, "source-hevc.mp4")).find(
      (s) => s.codec_type === "video",
    )?.codec_name;
    expect(sourceCodec).toBe("hevc");
    // Said out loud so a run without data/ cannot pass for one with it.
    console.log(`source clip: ${realClip ? "real Douyin download" : "synthetic pattern"}`);
  });

  it("keys the output by the hash of the spec", () => {
    expect(first.spec_sha256).toBe(specSha256(spec));
    expect(first.render_key).toBe(renderKey(AWEME, specSha256(spec)));
  });

  it("is as long as the spec, to within one frame", () => {
    const video = probe(output).find((s) => s.codec_type === "video");
    expect(video?.codec_name).toBe("h264");
    expect([video?.width, video?.height]).toEqual([1080, 1920]);
    expect(Math.abs(Number(video?.nb_frames) - durationInFrames(spec))).toBeLessThanOrEqual(1);
    expect(Math.abs(Number(video?.duration) - 3)).toBeLessThanOrEqual(1 / FPS + 1e-3);
  });

  it("has an audio stream", () => {
    const audio = probe(output).find((s) => s.codec_type === "audio");
    expect(audio?.codec_name).toBe("aac");
    expect(Math.abs(Number(audio?.duration) - 3)).toBeLessThan(0.1);
  });

  it("paints the cover in the colour of the spec", () => {
    // Opacity 100: the cover hides the picture, so its colour is exactly the spec's,
    // allowing for the lossy encode. A corner, clear of the text.
    const frame = decodeFrame(output, 1, 1080, 1920);
    const [r, g, b] = frame.at(COVER.x + 6, COVER.y + 6);
    COVER_COLOR.forEach((want, i) => expect(Math.abs([r, g, b][i] - want)).toBeLessThanOrEqual(10));
  });

  it("draws the picture outside the cover", () => {
    const frame = decodeFrame(output, 1, 1080, 1920);
    const [r, g, b] = frame.at(540, 300);
    const isCoverColour = COVER_COLOR.every((want, i) => Math.abs([r, g, b][i] - want) <= 10);
    expect(isCoverColour).toBe(false);
  });

  it("draws the caption in white inside the cover", () => {
    const frame = decodeFrame(output, 1, 1080, 1920);
    let white = 0;
    for (let y = COVER.y; y < COVER.y + COVER.h; y++) {
      for (let x = COVER.x; x < COVER.x + COVER.w; x++) {
        const [r, g, b] = frame.at(x, y);
        if (r > 220 && g > 220 && b > 220) white += 1;
      }
    }
    expect(white).toBeGreaterThan(500);
  });

  it("shows no caption before its cue starts", async () => {
    // A second spec, whose caption begins at 2 s, shows only the cover at 1 s.
    const late: EditSpec = {
      ...spec,
      captions: [{ ...spec.captions[0], start_ms: 2000, end_ms: 3000 }],
    };
    const lateKey = `work/${AWEME}/r2/edit-spec.json`;
    const file = path.join(dir, "late.json");
    fs.writeFileSync(file, JSON.stringify(late));
    await store.uploadFile(lateKey, file, "application/json");
    const out = await render({ aweme_id: AWEME, spec_key: lateKey });
    const lateFile = path.join(dir, "late.mp4");
    await store.download(out.render_key, lateFile);
    const frame = decodeFrame(lateFile, 1, 1080, 1920);
    let white = 0;
    for (let y = COVER.y; y < COVER.y + COVER.h; y++) {
      for (let x = COVER.x; x < COVER.x + COVER.w; x++) {
        const [r, g, b] = frame.at(x, y);
        if (r > 220 && g > 220 && b > 220) white += 1;
      }
    }
    expect(white).toBe(0);
    expect(frameAt(2000, FPS)).toBe(60);
  });

  it("renders the same spec only once, however often it is asked", async () => {
    const before = invocations.count(first.spec_sha256);
    const uploadsBefore = uploads.length;
    const again = await render({ aweme_id: AWEME, spec_key: specKey });
    const andAgain = await render({ aweme_id: AWEME, spec_key: specKey });
    expect(again).toEqual(first);
    expect(andAgain).toEqual(first);
    expect(before).toBe(1);
    expect(invocations.count(first.spec_sha256)).toBe(1);
    expect(uploads).toHaveLength(uploadsBefore);
  });
});
