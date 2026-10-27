import { createHash } from "node:crypto";
import * as fs from "node:fs";
import { ApplicationFailure } from "@temporalio/activity";
import type { EditSpec } from "../../src/edit-spec";
import type { ObjectStore } from "../../src/object-store";

/** An object store in memory, for the logic around it. The renderer's contact with a real
 * store (its policy, its notion of "missing") is checked by the integration suite. */
export class MemoryStore implements ObjectStore {
  readonly objects = new Map<string, Buffer>();
  readonly uploads: string[] = [];
  readonly downloads: string[] = [];

  put(key: string, data: Buffer | string): void {
    this.objects.set(key, Buffer.from(data));
  }

  private need(key: string): Buffer {
    const data = this.objects.get(key);
    if (data === undefined) {
      throw ApplicationFailure.nonRetryable(`artifact ${key} is missing`, "ArtifactMissing");
    }
    return data;
  }

  async getBytes(key: string): Promise<Buffer> {
    return this.need(key);
  }

  async download(key: string, destination: string): Promise<void> {
    this.downloads.push(key);
    fs.writeFileSync(destination, this.need(key));
  }

  async exists(key: string): Promise<boolean> {
    return this.objects.has(key);
  }

  async uploadFile(key: string, file: string): Promise<void> {
    this.uploads.push(key);
    this.objects.set(key, fs.readFileSync(file));
  }
}

export const AWEME = "7001";
export const SPEC_KEY = `work/${AWEME}/r1/edit-spec.json`;
export const SOURCE_BYTES = Buffer.from("pretend this is a video");
export const AUDIO_BYTES = Buffer.from("pretend this is a dubbed track");
export const FONT_NAME = "BeVietnamPro-SemiBold.ttf";
export const FONT_BYTES = Buffer.from("pretend this is a font");

export function sha256(data: Buffer): string {
  return createHash("sha256").update(data).digest("hex");
}

export function makeSpec(over: Partial<EditSpec> = {}): EditSpec {
  return {
    source_key: `work/${AWEME}/r0/source.mp4`,
    audio_key: `work/${AWEME}/r1/dubbed.wav`,
    audio_sha256: sha256(AUDIO_BYTES),
    script_version: 2,
    width: 1080,
    height: 1920,
    fps: 30,
    duration_ms: 3000,
    cover: {
      region: { x: 40, y: 1500, w: 1000, h: 120 },
      color: "#101010",
      opacity_pct: 92,
    },
    caption_box: { x: 40, y: 1500, w: 1000, h: 120 },
    captions: [
      { segment_id: 0, start_ms: 0, end_ms: 3000, text: "Xin chào các bạn", font_px: 48 },
    ],
    font: FONT_NAME,
    font_sha256: sha256(FONT_BYTES),
    ...over,
  };
}

/** A store holding a spec and the two files it names. */
export function storeWith(spec: EditSpec = makeSpec(), specKey = SPEC_KEY): MemoryStore {
  const store = new MemoryStore();
  store.put(specKey, JSON.stringify(spec));
  store.put(spec.source_key, SOURCE_BYTES);
  store.put(spec.audio_key, AUDIO_BYTES);
  return store;
}
