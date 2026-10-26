// ffmpeg / ffprobe helpers for the tests that look at real rendered files.
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export const REPO_ROOT = path.resolve(__dirname, "../../../..");
export const FONT_PATH = path.join(REPO_ROOT, "assets/fonts/BeVietnamPro-SemiBold.ttf");

export interface StreamInfo {
  codec_type: string;
  codec_name: string;
  width?: number;
  height?: number;
  nb_frames?: string;
  duration?: string;
  r_frame_rate?: string;
}

export function probe(file: string, countFrames = false): StreamInfo[] {
  const out = execFileSync(
    "ffprobe",
    ["-v", "error", ...(countFrames ? ["-count_frames"] : []), "-print_format", "json", "-show_streams", file],
    { encoding: "utf8" },
  );
  return JSON.parse(out).streams as StreamInfo[];
}

export interface Frame {
  width: number;
  height: number;
  rgb: Buffer;
  at(x: number, y: number): [number, number, number];
}

/** One decoded frame as RGB, `seconds` into the file. */
export function frameAt(file: string, seconds: number, width: number, height: number): Frame {
  const rgb = execFileSync(
    "ffmpeg",
    [
      "-nostdin", "-v", "error", "-ss", String(seconds), "-i", file,
      "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], // prettier-ignore
    { maxBuffer: width * height * 3 + 1024 },
  );
  if (rgb.length !== width * height * 3) throw new Error("ffmpeg returned a short frame");
  return {
    width,
    height,
    rgb,
    at: (x, y) => {
      const i = (y * width + x) * 3;
      return [rgb[i], rgb[i + 1], rgb[i + 2]];
    },
  };
}

/** A 3 second HEVC clip, 1080x1920 at 30 fps, no audio. Cut from a real Douyin download
 * under data/ when there is one (the source files there are what production feeds the
 * renderer, re-encoded to HEVC, the codec Douyin also serves); otherwise a synthetic
 * pattern, and `real` says which it was so a report cannot claim more than ran. */
export function makeHevcFixture(into: string): { file: string; real: boolean } {
  const file = path.join(into, "source-hevc.mp4");
  const real = findDouyinClip(path.join(REPO_ROOT, "data"));
  const common = ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "30", "-tag:v", "hvc1"];
  if (real) {
    execFileSync(
      "ffmpeg",
      ["-nostdin", "-y", "-v", "error", "-ss", "1", "-t", "3", "-i", real, "-an", "-r", "30",
        "-vf", "scale=1080:1920", "-pix_fmt", "yuv420p", ...common, file], // prettier-ignore
    );
  } else {
    execFileSync(
      "ffmpeg",
      ["-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=3",
        "-pix_fmt", "yuv420p", ...common, file], // prettier-ignore
    );
  }
  return { file, real: real !== null };
}

function findDouyinClip(root: string): string | null {
  const walk = (dir: string): string | null => {
    if (!fs.existsSync(dir)) return null;
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        const found = walk(full);
        if (found) return found;
      } else if (entry.name.endsWith(".mp4")) {
        const video = probe(full).find((s) => s.codec_type === "video");
        const seconds = Number(video?.duration ?? 0);
        if (video && seconds >= 5 && video.width === 1080) return full;
      }
    }
    return null;
  };
  return walk(path.join(root, "douyin"));
}

export function makeTone(into: string, seconds: number): string {
  const file = path.join(into, "audio.wav");
  execFileSync("ffmpeg", [
    "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", `sine=frequency=300:duration=${seconds}`,
    "-ar", "24000", "-ac", "1", file,
  ]); // prettier-ignore
  return file;
}

export function tmpDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}
